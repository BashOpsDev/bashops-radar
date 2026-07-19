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


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


# Historical repository evidence is ephemeral and currently cached per process.
EVIDENCE_CACHE_TTL_HOURS = _bounded_int_env("EVIDENCE_CACHE_TTL_HOURS", 24, 1, 168)
EVIDENCE_CACHE_MAX_ENTRIES = _bounded_int_env("EVIDENCE_CACHE_MAX_ENTRIES", 256, 16, 2048)
EVIDENCE_CACHE_STALE_HOURS = _bounded_int_env("EVIDENCE_CACHE_STALE_HOURS", 72, 0, 336)
EVIDENCE_OPEN_PR_SAMPLE = _bounded_int_env("EVIDENCE_OPEN_PR_SAMPLE", 50, 1, 100)
EVIDENCE_CLOSED_PR_SAMPLE = _bounded_int_env("EVIDENCE_CLOSED_PR_SAMPLE", 100, 2, 100)
EVIDENCE_RELEASE_SAMPLE = _bounded_int_env("EVIDENCE_RELEASE_SAMPLE", 20, 1, 100)
EVIDENCE_MIN_CLOSED_PR_SAMPLE = min(
    EVIDENCE_CLOSED_PR_SAMPLE,
    _bounded_int_env("EVIDENCE_MIN_CLOSED_PR_SAMPLE", 5, 2, 25),
)
