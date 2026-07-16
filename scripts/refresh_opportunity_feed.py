"""Refresh the bounded opportunity feed for a future Railway Cron job."""

import sys

from opportunity_service import refresh_opportunity_feed


def main() -> int:
    result = refresh_opportunity_feed(force=True)
    print(
        "Opportunity feed refresh: "
        f"status={result['status']} updated={result['updated']} failed={result['failed']}"
    )
    return 0 if result["status"] in {"refreshed", "cache_hit"} else 1


if __name__ == "__main__":
    sys.exit(main())
