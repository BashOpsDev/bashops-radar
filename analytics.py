import csv
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict

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
    unique_visitors = len(set(row.get("ip", "") for row in rows if row.get("ip")))
    repeat_visitors = 0

    visitor_counts = Counter(row.get("ip", "") for row in rows if row.get("ip"))
    repeat_visitors = len([ip for ip, count in visitor_counts.items() if count > 1])

    scores = []
    for row in rows:
        try:
            scores.append(int(float(row.get("score", 0))))
        except Exception:
            pass

    average_score = round(sum(scores) / len(scores), 1) if scores else 0
    highest_score = max(scores) if scores else 0

    repo_counts = Counter()
    issue_counts = Counter()
    repo_scores = {}
    daily_counts = defaultdict(int)

    for row in rows:
        repo = row.get("repo", "unknown")
        issue = row.get("best_issue", "")
        score = row.get("score", "0")

        repo_counts[repo] += 1

        if issue:
            issue_counts[issue] += 1

        try:
            repo_scores[repo] = int(float(score))
        except Exception:
            repo_scores[repo] = 0

        try:
            dt = datetime.fromisoformat(row.get("timestamp", ""))
            day = dt.strftime("%d %b")
            daily_counts[day] += 1
        except Exception:
            pass

        row["pretty_time"] = format_time(row.get("timestamp", ""))

    top_repos = [
        {
            "repo": repo,
            "count": count,
            "score": repo_scores.get(repo, 0),
        }
        for repo, count in repo_counts.most_common(10)
    ]

    top_issues = issue_counts.most_common(10)
    daily_activity = list(daily_counts.items())[-7:]

best_opportunities = []

seen = set()
for row in sorted(rows, key=lambda x: x.get("score", "0"), reverse=True):
    repo = row.get("repo", "")
    if repo and repo not in seen:
        seen.add(repo)
        best_opportunities.append({
            "repo": repo,
            "score": row.get("score", "0"),
            "best_issue": row.get("best_issue", ""),
            "time": row.get("pretty_time", ""),
        })

best_opportunities = best_opportunities[:5]

    return {
        "rows": rows,
        "total_analyses": total_analyses,
        "unique_repos": unique_repos,
        "unique_visitors": unique_visitors,
        "repeat_visitors": repeat_visitors,
        "average_score": average_score,
        "highest_score": highest_score,
        "top_repos": top_repos,
        "top_issues": top_issues,
        "daily_activity": daily_activity,
    }