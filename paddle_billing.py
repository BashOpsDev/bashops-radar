import hashlib
import hmac
import json
import time

import config
from database import SessionLocal
from models import User


class PaddleBillingNotConfigured(Exception):
    pass


class PaddleSignatureError(Exception):
    pass


ACTIVE_STATUSES = {"active", "trialing"}
INACTIVE_STATUSES = {"canceled", "cancelled", "past_due", "paused"}


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

    if abs(int(time.time()) - timestamp_int) > 300:
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


def _find_user(db, data: dict):
    custom = _custom_data(data)
    user_id = custom.get("user_id")
    if user_id:
        try:
            user = db.query(User).filter(User.id == int(user_id)).first()
        except (TypeError, ValueError):
            user = None
        if user:
            return user

    subscription_id = data.get("subscription_id") or data.get("id")
    if subscription_id:
        user = db.query(User).filter(User.paddle_subscription_id == subscription_id).first()
        if user:
            return user

    email = _customer_email(data).strip().lower()
    if email:
        return db.query(User).filter(User.email == email).first()

    return None


def _sync_paddle_ids(user: User, data: dict) -> None:
    customer_id = data.get("customer_id")
    subscription_id = data.get("subscription_id") or data.get("id")

    if customer_id:
        user.paddle_customer_id = customer_id
    if subscription_id:
        user.paddle_subscription_id = subscription_id


def handle_paddle_webhook_event(event: dict) -> None:
    event_type = event.get("event_type") or event.get("type")
    data = _event_data(event)

    if event_type not in {
        "transaction.completed",
        "subscription.created",
        "subscription.activated",
        "subscription.updated",
        "subscription.canceled",
        "subscription.past_due",
    }:
        return

    db = SessionLocal()
    try:
        user = _find_user(db, data)
        if not user:
            return

        status = data.get("status") or (
            "past_due" if event_type == "subscription.past_due" else
            "canceled" if event_type == "subscription.canceled" else
            "active"
        )

        _sync_paddle_ids(user, data)
        user.subscription_status = status

        if event_type == "transaction.completed" or status in ACTIVE_STATUSES:
            user.plan = "pro"
        elif event_type in {"subscription.canceled", "subscription.past_due"} or status in INACTIVE_STATUSES:
            user.plan = "free"

        db.commit()
    finally:
        db.close()
