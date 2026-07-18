import os


FREE_PLAN = "free"
RADAR_PRO_PLAN = "radar_pro"
MAINTAINER_PRO_PLAN = "maintainer_pro"
LEGACY_RADAR_PRO_PLAN = "pro"

RADAR_MONTHLY_PRICE = 29
RADAR_ANNUAL_PRICE = 290
MAINTAINER_MONTHLY_PRICE = 99
MAINTAINER_ANNUAL_PRICE = 990

RADAR_ANNUAL_SAVINGS = (RADAR_MONTHLY_PRICE * 12) - RADAR_ANNUAL_PRICE
MAINTAINER_ANNUAL_SAVINGS = (MAINTAINER_MONTHLY_PRICE * 12) - MAINTAINER_ANNUAL_PRICE

PADDLE_RADAR_MONTHLY_PRICE_ID = os.getenv("PADDLE_RADAR_MONTHLY_PRICE_ID", "")
PADDLE_RADAR_ANNUAL_PRICE_ID = os.getenv("PADDLE_RADAR_ANNUAL_PRICE_ID", "")
PADDLE_MAINTAINER_MONTHLY_PRICE_ID = os.getenv("PADDLE_MAINTAINER_MONTHLY_PRICE_ID", "")
PADDLE_MAINTAINER_ANNUAL_PRICE_ID = os.getenv("PADDLE_MAINTAINER_ANNUAL_PRICE_ID", "")


def price_id(product: str, billing_period: str) -> str:
    return {
        ("radar", "monthly"): PADDLE_RADAR_MONTHLY_PRICE_ID,
        ("radar", "annual"): PADDLE_RADAR_ANNUAL_PRICE_ID,
        ("maintainer", "monthly"): PADDLE_MAINTAINER_MONTHLY_PRICE_ID,
        ("maintainer", "annual"): PADDLE_MAINTAINER_ANNUAL_PRICE_ID,
    }.get((product, billing_period), "")


def configured_price_products() -> dict[str, str]:
    """Map configured Paddle price IDs to products, excluding ambiguous IDs."""
    configured = {
        PADDLE_RADAR_MONTHLY_PRICE_ID: "radar",
        PADDLE_RADAR_ANNUAL_PRICE_ID: "radar",
        PADDLE_MAINTAINER_MONTHLY_PRICE_ID: "maintainer",
        PADDLE_MAINTAINER_ANNUAL_PRICE_ID: "maintainer",
    }
    values = [
        PADDLE_RADAR_MONTHLY_PRICE_ID,
        PADDLE_RADAR_ANNUAL_PRICE_ID,
        PADDLE_MAINTAINER_MONTHLY_PRICE_ID,
        PADDLE_MAINTAINER_ANNUAL_PRICE_ID,
    ]
    duplicates = {value for value in values if value and values.count(value) > 1}
    return {
        price: product
        for price, product in configured.items()
        if price and price not in duplicates
    }


def template_context() -> dict[str, int]:
    return {
        "radar_monthly_price": RADAR_MONTHLY_PRICE,
        "radar_annual_price": RADAR_ANNUAL_PRICE,
        "radar_annual_savings": RADAR_ANNUAL_SAVINGS,
        "maintainer_monthly_price": MAINTAINER_MONTHLY_PRICE,
        "maintainer_annual_price": MAINTAINER_ANNUAL_PRICE,
        "maintainer_annual_savings": MAINTAINER_ANNUAL_SAVINGS,
    }
