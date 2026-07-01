import os

APP_NAME = "BashOps Radar"
APP_VERSION = "1.0"
PUBLIC_MODE = False
SITE_URL = os.getenv("SITE_URL", "https://bashops.site")

ADMIN_EMAILS = [
    email.strip().lower()
    for email in os.getenv("ADMIN_EMAILS", os.getenv("ADMIN_EMAIL", "")).split(",")
    if email.strip()
]

PRO_PRICE_USD = int(os.getenv("PRO_PRICE_USD", "19"))

PADDLE_API_KEY = os.getenv("PADDLE_API_KEY", "")
PADDLE_CLIENT_TOKEN = os.getenv("PADDLE_CLIENT_TOKEN", "")
PADDLE_PRICE_ID = os.getenv("PADDLE_PRICE_ID", "")
PADDLE_WEBHOOK_SECRET = os.getenv("PADDLE_WEBHOOK_SECRET", "")
PADDLE_ENV = os.getenv("PADDLE_ENV", "production")

paddle_configured = bool(PADDLE_CLIENT_TOKEN and PADDLE_PRICE_ID)
