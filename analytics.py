import csv
from pathlib import Path
from datetime import datetime, timezone

ANALYTICS_FILE = Path("analytics.csv")


def track_analysis(repo, score, best_issue, request):
    file_exists = ANALYTICS_FILE.exists()

    ip = request.client.host if request.client else "unknown"
    user_agent = request.headers.get("user-agent", "unknown")

    with ANALYTICS_FILE.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow([
                "timestamp",
                "repo",
                "score",
                "best_issue",
                "ip",
                "user_agent",
            ])

        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            repo,
            score,
            best_issue or "",
            ip,
            user_agent,
        ])


def read_analytics():
    if not ANALYTICS_FILE.exists():
        return []

    with ANALYTICS_FILE.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))