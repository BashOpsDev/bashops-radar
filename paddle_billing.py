import hashlib
import hmac
import json
import logging
import re
import time
from urllib.parse import urlparse

import requests
from sqlalchemy import func

import config
from database import SessionLocal
from models import User


logger = logging.getLogger(__name__)


class PaddleBillingNotConfigured(Exception):
    pass


class PaddleSignatureError(Exception):
    pass


class PaddlePortalError(Exception):
    pass


ACTIVE_STATUSES = {"active", "trialing"}
INACTIVE_STATUSES = {"canceled", "cancelled", "past_due", "paused", "inactive"}
SIGNATURE_TOLERANCE_SECONDS = 5
SUPPORTED_EVENTS = {
    "transaction.completed",
    "subscription.created",
    "subscription.activated",
    "subscription.updated",
    "subscription.canceled",
    "subscription.past_due",
    "subscription.paused",
    "subscription.resumed",
    "subscription.trialing",
}


def _header_value(headers, name: str) -> str:
    return headers.get(name) or headers.get(name.lower()) or ""


def verify_paddle_webhook(request_body: bytes, headers):
    if not config.PADDLE_WEBHOOK_SECRET:
        raise PaddleBillingNotConfigured("PADDLE_WEBHOOK_SECRET is not set.")

    signature_header = _header_value(headers, "Paddle-Signature")
    parts = {}
    for item in signature_header.split(";"):
        if "=" in item:
            key, value = item.split("=", 1)
            parts[key.strip()] = value.strip()

    timestamp = parts.get("ts")
    signature = parts.get("h1")
    if not timestamp or not signature:
        raise PaddleSignatureError("Missing Paddle webhook signature.")

    try:
        timestamp_int = int(timestamp)
    except ValueError as exc:
        raise PaddleSignatureError("Invalid Paddle webhook timestamp.") from exc

    if abs(int(time.time()) - timestamp_int) > SIGNATURE_TOLERANCE_SECONDS:
        raise PaddleSignatureError("Expired Paddle webhook timestamp.")

    signed_payload = f"{timestamp}:".encode("utf-8") + request_body
    expected = hmac.new(
        config.PADDLE_WEBHOOK_SECRET.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise PaddleSignatureError("Invalid Paddle webhook signature.")

    return json.loads(request_body.decode("utf-8"))


def _event_data(event: dict) -> dict:
    data = event.get("data") or {}
    return data if isinstance(data, dict) else {}


def _custom_data(data: dict) -> dict:
    custom = data.get("custom_data") or {}
    return custom if isinstance(custom, dict) else {}


def _customer_email(data: dict) -> str:
    customer = data.get("customer") or {}
    if isinstance(customer, dict) and customer.get("email"):
        return customer["email"]

    billing_details = data.get("billing_details") or {}
    if isinstance(billing_details, dict) and billing_details.get("email"):
        return billing_details["email"]

    return data.get("customer_email") or data.get("email") or ""


def _subscription_id(event_type: str, data: dict) -> str:
    if data.get("subscription_id"):
        return str(data["subscription_id"])
    if event_type.startswith("subscription.") and data.get("id"):
        return str(data["id"])
    return ""


def _find_user(db, event_type: str, data: dict):
    custom = _custom_data(data)
    user_id = custom.get("user_id")
    if user_id:
        try:
            user = db.query(User).filter(User.id == int(user_id)).first()
        except (TypeError, ValueError):
            user = None
        if user:
            return user

    subscription_id = _subscription_id(event_type, data)
    if subscription_id:
        user = db.query(User).filter(User.paddle_subscription_id == subscription_id).first()
        if user:
            return user
        user = db.query(User).filter(User.maintainer_paddle_subscription_id == subscription_id).first()
        if user:
            return user

    email = _customer_email(data).strip().lower()
    if email:
        return db.query(User).filter(func.lower(User.email) == email).first()

    return None


def _price_ids(data: dict) -> set[str]:
    price_ids = set()
    items = data.get("items") or []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            price = item.get("price") or {}
            if isinstance(price, dict) and price.get("id"):
                price_ids.add(str(price["id"]))
            if item.get("price_id"):
                price_ids.add(str(item["price_id"]))

    details = data.get("details") or {}
    line_items = details.get("line_items") or [] if isinstance(details, dict) else []
    if isinstance(line_items, list):
        for item in line_items:
            if isinstance(item, dict) and item.get("price_id"):
                price_ids.add(str(item["price_id"]))
    return price_ids


def _matched_products(data: dict) -> set[str]:
    price_ids = _price_ids(data)
    known_prices = {
        "radar": config.PADDLE_PRICE_ID,
        "maintainer": config.PADDLE_MAINTAINER_PRICE_ID,
    }
    duplicate_prices = {
        price_id
        for price_id in known_prices.values()
        if price_id and list(known_prices.values()).count(price_id) > 1
    }
    return {
        product
        for product, price_id in known_prices.items()
        if price_id and price_id not in duplicate_prices and price_id in price_ids
    }


def _status_for_event(event_type: str, data: dict) -> str:
    status = str(data.get("status") or "").strip().lower()
    if status:
        return status
    return {
        "subscription.canceled": "canceled",
        "subscription.past_due": "past_due",
        "subscription.paused": "paused",
        "subscription.trialing": "trialing",
        "subscription.resumed": "active",
        "subscription.activated": "active",
        "subscription.created": "active",
        "transaction.completed": "completed",
    }.get(event_type, "")


def _apply_product_state(user: User, product: str, event_type: str, data: dict) -> None:
    subscription_id = _subscription_id(event_type, data)
    status = _status_for_event(event_type, data)
    grants_access = event_type == "transaction.completed" or status in ACTIVE_STATUSES
    removes_access = status in INACTIVE_STATUSES

    if product == "radar":
        if subscription_id:
            user.paddle_subscription_id = subscription_id
        if status:
            user.subscription_status = status
        if grants_access:
            user.plan = "pro"
        elif removes_access:
            user.plan = "free"
        return

    if subscription_id:
        user.maintainer_paddle_subscription_id = subscription_id
    if status:
        user.maintainer_subscription_status = status
    if grants_access:
        user.maintainer_pilot_access = True
    elif removes_access:
        user.maintainer_pilot_access = False


def _safe_log_value(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", str(value or ""))[:80]
    if len(cleaned) <= 12:
        return cleaned or "none"
    return f"{cleaned[:4]}...{cleaned[-6:]}"


def handle_paddle_webhook_event(event: dict) -> set[str]:
    event_type = str(event.get("event_type") or event.get("type") or "")
    if event_type not in SUPPORTED_EVENTS:
        return set()

    data = _event_data(event)
    products = _matched_products(data)
    if not products:
        logger.info(
            "Ignoring Paddle event type=%s subscription=%s with no recognized price",
            _safe_log_value(event_type),
            _safe_log_value(_subscription_id(event_type, data)),
        )
        return set()

    db = SessionLocal()
    try:
        user = _find_user(db, event_type, data)
        if not user:
            return set()

        customer_id = data.get("customer_id")
        if customer_id:
            user.paddle_customer_id = str(customer_id)

        for product in products:
            _apply_product_state(user, product, event_type, data)

        db.commit()
        return products
    finally:
        db.close()


def _paddle_api_base_url() -> str:
    return "https://sandbox-api.paddle.com" if config.PADDLE_ENV == "sandbox" else "https://api.paddle.com"


def create_customer_portal_url(customer_id: str) -> str:
    if not config.PADDLE_API_KEY:
        raise PaddleBillingNotConfigured("PADDLE_API_KEY is not set.")
    if not re.fullmatch(r"ctm_[a-z0-9]{26}", customer_id or ""):
        raise PaddlePortalError("Invalid customer reference.")

    try:
        response = requests.post(
            f"{_paddle_api_base_url()}/customers/{customer_id}/portal-sessions",
            headers={
                "Authorization": f"Bearer {config.PADDLE_API_KEY}",
                "Content-Type": "application/json",
                "Paddle-Version": "1",
            },
            json={},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        portal_url = payload["data"]["urls"]["general"]["overview"]
    except Exception as exc:
        raise PaddlePortalError("Paddle customer portal is temporarily unavailable.") from exc

    parsed = urlparse(str(portal_url))
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".paddle.com"):
        raise PaddlePortalError("Paddle returned an invalid portal URL.")
    return str(portal_url)
