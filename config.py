import os

APP_NAME = "BashOps Radar"
APP_VERSION = "1.0"
PUBLIC_MODE = False
SITE_URL = os.getenv("SITE_URL", "https://bashops.site")

# Only these accounts (if set) can view the global /admin/analytics dashboard.
# Everyone else's pipeline/analytics views are scoped to their own account.
ADMIN_EMAILS = [
    email.strip().lower()
    for email in os.getenv("ADMIN_EMAILS", os.getenv("ADMIN_EMAIL", "")).split(",")
    if email.strip()
]

# --- Pricing -----------------------------------------------------------
# Single source of truth for the displayed price. Changing PRO_PRICE_USD
# only updates what's shown in templates — it does NOT change what Stripe
# actually charges. The real amount is whatever Price object
# STRIPE_PRO_PRICE_ID points to in the Stripe Dashboard. If you change one,
# change the other so they stay in sync.
PRO_PRICE_USD = int(os.getenv("PRO_PRICE_USD", "19"))

# --- Stripe --------------------------------------------------------------
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRO_PRICE_ID = os.getenv("STRIPE_PRO_PRICE_ID", os.getenv("STRIPE_PRICE_ID_PRO", ""))
