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


def format_time(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%d %b %Y • %H:%M UTC")
    except Exception:
        return value


def analytics_summary():
    rows = read_analytics()

    total_analyses = len(rows)
    unique_repos = len(set(row.get("repo", "") for row in rows if row.get("repo")))

    scores = []
    for row in rows:
        try:
            scores.append(int(row.get("score", 0)))
        except Exception:
            pass

    average_score = round(sum(scores) / len(scores), 1) if scores else 0
    highest_score = max(scores) if scores else 0

    repo_counts = {}
    issue_counts = {}

    for row in rows:
        repo = row.get("repo", "unknown")
        issue = row.get("best_issue", "")

        repo_counts[repo] = repo_counts.get(repo, 0) + 1

        if issue:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1

        row["pretty_time"] = format_time(row.get("timestamp", ""))

    top_repos = sorted(repo_counts.items(), key=lambda x: x[1], reverse=True)[:10]
    top_issues = sorted(issue_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    return {
        "rows": rows,
        "total_analyses": total_analyses,
        "unique_repos": unique_repos,
        "average_score": average_score,
        "highest_score": highest_score,
        "top_repos": top_repos,
        "top_issues": top_issues,
    }