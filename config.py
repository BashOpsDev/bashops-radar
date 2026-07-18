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

PADDLE_API_KEY = os.getenv("PADDLE_API_KEY", "")
PADDLE_CLIENT_TOKEN = os.getenv("PADDLE_CLIENT_TOKEN", "")
PADDLE_WEBHOOK_SECRET = os.getenv("PADDLE_WEBHOOK_SECRET", "")
PADDLE_ENV = os.getenv("PADDLE_ENV", "production")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "BashOps Radar")

email_configured = bool(RESEND_API_KEY and EMAIL_FROM)

GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
GITHUB_OAUTH_REDIRECT_URI = os.getenv(
    "GITHUB_OAUTH_REDIRECT_URI",
    f"{SITE_URL}/auth/github/callback",
)

github_oauth_configured = bool(GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET and GITHUB_OAUTH_REDIRECT_URI)

MAINTAINER_ENABLED = os.getenv("MAINTAINER_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
