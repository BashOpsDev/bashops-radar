"""
Stripe billing integration.

Setup required in the Stripe Dashboard before this works (test mode first):

1. Create a Product ("BashOps Radar Pro") with a recurring monthly Price.
   Copy that Price's ID (starts with `price_`) into STRIPE_PRO_PRICE_ID.
2. Developers > API keys: copy the Secret key into STRIPE_SECRET_KEY and
   the Publishable key into STRIPE_PUBLISHABLE_KEY.
3. Developers > Webhooks > Add endpoint:
     URL:    https://<your-domain>/billing/webhook
     Events: checkout.session.completed
             customer.subscription.updated
             customer.subscription.deleted
   Copy the "Signing secret" (starts with `whsec_`) into
   STRIPE_WEBHOOK_SECRET.
4. Developers > Billing Portal: turn it on (needed for /billing/portal).

None of the routes in this file will work until STRIPE_SECRET_KEY and
STRIPE_PRO_PRICE_ID are set — they raise a clear error instead of failing
silently so a misconfigured deploy is obvious immediately rather than
looking like a successful checkout that never upgrades anyone.
"""

import stripe

import config
from database import SessionLocal
from models import User

stripe.api_key = config.STRIPE_SECRET_KEY


class BillingNotConfigured(Exception):
    pass


def _require_configured():
    if not config.STRIPE_SECRET_KEY or not config.STRIPE_PRO_PRICE_ID:
        raise BillingNotConfigured(
            "Stripe is not configured yet. Set STRIPE_SECRET_KEY and "
            "STRIPE_PRO_PRICE_ID (see billing.py header for setup steps)."
        )


def create_checkout_session(user: User, success_url: str, cancel_url: str) -> str:
    """Creates a Stripe Checkout Session for the Pro monthly plan and
    returns the URL to redirect the user to."""
    _require_configured()

    session_kwargs = dict(
        mode="subscription",
        line_items=[{"price": config.STRIPE_PRO_PRICE_ID, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(user.id),
    )

    # Reuse the existing Stripe customer if this user has upgraded before
    # (e.g. re-subscribing after a cancellation) instead of creating a
    # duplicate customer record in Stripe.
    if user.stripe_customer_id:
        session_kwargs["customer"] = user.stripe_customer_id
    else:
        session_kwargs["customer_email"] = user.email

    session = stripe.checkout.Session.create(**session_kwargs)
    return session.url


def create_portal_session(user: User, return_url: str) -> str:
    """Creates a Stripe Billing Portal session so an existing Pro user can
    update their card or cancel, without you building that UI yourself."""
    _require_configured()

    if not user.stripe_customer_id:
        raise BillingNotConfigured(
            "This account has no Stripe customer yet — they need to "
            "complete checkout at least once before the billing portal "
            "is available."
        )

    session = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=return_url,
    )
    return session.url


def construct_webhook_event(payload: bytes, sig_header: str):
    """Verifies the webhook signature and returns the parsed Stripe event.
    Raises stripe.SignatureVerificationError on a bad/forged signature
    — callers must catch this and return 400, never process the payload."""
    if not config.STRIPE_WEBHOOK_SECRET:
        raise BillingNotConfigured("STRIPE_WEBHOOK_SECRET is not set.")

    return stripe.Webhook.construct_event(
        payload, sig_header, config.STRIPE_WEBHOOK_SECRET
    )


def handle_webhook_event(event) -> None:
    """
    Applies a verified Stripe event to our User table. This is the only
    place plan/stripe_* fields get written from Stripe's side — the
    checkout success redirect does NOT grant Pro by itself (a user could
    hit that URL without paying), only a verified webhook does.
    """
    event_type = event["type"]

    # event["data"]["object"] is a StripeObject in this SDK version, not a
    # plain dict — it does NOT support .get(), only attribute/bracket
    # access that raises on missing keys. .to_dict() converts it to an
    # actual dict so .get(..., default) is safe below.
    raw_object = event["data"]["object"]
    data = raw_object.to_dict() if hasattr(raw_object, "to_dict") else dict(raw_object)

    db = SessionLocal()
    try:
        if event_type == "checkout.session.completed":
            user_id = data.get("client_reference_id")
            if not user_id:
                return

            try:
                user_id = int(user_id)
            except (TypeError, ValueError):
                return

            user = db.query(User).filter(User.id == user_id).first()
            if not user:
                return

            user.stripe_customer_id = data.get("customer")
            user.stripe_subscription_id = data.get("subscription")
            user.plan = "pro"
            user.subscription_status = "active"
            db.commit()

        elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
            subscription_id = data.get("id")
            status = data.get("status")  # active, past_due, canceled, unpaid, trialing...

            user = (
                db.query(User)
                .filter(User.stripe_subscription_id == subscription_id)
                .first()
            )
            if not user:
                return

            user.subscription_status = status

            if event_type == "customer.subscription.deleted" or status in ("canceled", "unpaid"):
                user.plan = "free"
            elif status in ("active", "trialing"):
                user.plan = "pro"

            db.commit()
    finally:
        db.close()
