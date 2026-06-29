import csv
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict

ANALYTICS_FILE = Path("analytics.csv")
DEFAULT_STATUS = "New Target"

PIPELINE_STATUSES = [
    "New Target",
    "Researching",
    "Working",
    "PR Submitted",
    "PR Merged",
    "Founder Contacted",
    "Paid Sprint",
    "Retainer",
]

STATUS_PROGRESS = {
    "New Target": 12,
    "Researching": 25,
    "Working": 50,
    "PR Submitted": 70,
    "PR Merged": 85,
    "Founder Contacted": 95,
    "Paid Sprint": 100,
    "Retainer": 100,
}


def repo_links(repo_url: str, best_issue_url: str = ""):
    repo_url = (repo_url or "").strip()

    org_url = ""
    if "github.com/" in repo_url:
        parts = repo_url.split("github.com/")[-1].strip("/").split("/")
        if len(parts) >= 1:
            org_url = f"https://github.com/{parts[0]}"

    return {
        "repo_url": repo_url,
        "best_issue_url": best_issue_url or f"{repo_url}/issues",
        "org_url": org_url,
    }


def track_analysis(repo, score, best_issue, request, status=DEFAULT_STATUS, language="Unknown"):
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
                "status",
            ])

        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            repo,
            score,
            best_issue or "",
            ip,
            user_agent,
            status,
        ])


def read_analytics():
    if not ANALYTICS_FILE.exists():
        return []

    with ANALYTICS_FILE.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        if not row.get("status"):
            row["status"] = DEFAULT_STATUS

        row["progress"] = STATUS_PROGRESS.get(row["status"], 12)
        row["pretty_time"] = format_time(row.get("timestamp", ""))

        repo = row.get("repo", "")
        best_issue = row.get("best_issue", "")

        row["links"] = repo_links(repo, best_issue)
        row["pitch"] = generate_pitch(repo, best_issue)

        row["language"] = row.get("language") or "Unknown"
        row["difficulty"] = row.get("difficulty") or estimate_difficulty(row.get("score", 0))
        row["merge_probability"] = row.get("merge_probability") or estimate_merge_probability(row.get("score", 0))
        row["estimated_time"] = row.get("estimated_time") or estimate_completion_time(row.get("score", 0))

    return rows


def write_analytics(rows):
    with ANALYTICS_FILE.open("w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "timestamp",
            "repo",
            "score",
            "best_issue",
            "ip",
            "user_agent",
            "status",
        ]

        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            writer.writerow({
                "timestamp": row.get("timestamp", ""),
                "repo": row.get("repo", ""),
                "score": row.get("score", ""),
                "best_issue": row.get("best_issue", ""),
                "ip": row.get("ip", ""),
                "user_agent": row.get("user_agent", ""),
                "status": row.get("status", DEFAULT_STATUS),
            })


def update_pipeline_status(repo, status):
    if status not in PIPELINE_STATUSES:
        return False

    rows = read_analytics()
    updated = False

    for row in rows:
        if row.get("repo") == repo:
            row["status"] = status
            updated = True

    if updated:
        write_analytics(rows)

    return updated


def format_time(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt.strftime("%d %b %Y • %H:%M UTC")
    except Exception:
        return value


def estimate_difficulty(score):
    try:
        score = int(float(score))
    except Exception:
        score = 0

    if score >= 80:
        return "Easy"
    if score >= 60:
        return "Medium"
    return "Hard"


def estimate_merge_probability(score):
    try:
        score = int(float(score))
    except Exception:
        score = 0

    if score >= 80:
        return "High merge probability"
    if score >= 60:
        return "Medium merge probability"
    return "Low merge probability"


def estimate_completion_time(score):
    try:
        score = int(float(score))
    except Exception:
        score = 0

    if score >= 80:
        return "2–4h"
    if score >= 60:
        return "1 day"
    return "2 days"


def analytics_summary():
    rows = read_analytics()

    total_analyses = len(rows)
    unique_repos = len(set(row.get("repo", "") for row in rows if row.get("repo")))

    visitor_counts = Counter(row.get("ip", "") for row in rows if row.get("ip"))
    unique_visitors = len(visitor_counts)
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
    status_counts = Counter()

    for row in rows:
        repo = row.get("repo", "unknown")
        issue = row.get("best_issue", "")
        score = row.get("score", "0")
        status = row.get("status", DEFAULT_STATUS)

        repo_counts[repo] += 1
        status_counts[status] += 1

        if issue:
            issue_counts[issue] += 1

        try:
            repo_scores[repo] = int(float(score))
        except Exception:
            repo_scores[repo] = 0

        try:
            dt = datetime.fromisoformat(row.get("timestamp", ""))
            daily_counts[dt.strftime("%d %b")] += 1
        except Exception:
            pass

    top_repos = [
        {"repo": repo, "count": count, "score": repo_scores.get(repo, 0)}
        for repo, count in repo_counts.most_common(10)
    ]

    daily_activity = [
        {"day": day, "count": count, "bar": "█" * min(count, 20)}
        for day, count in list(daily_counts.items())[-7:]
    ]

    best_opportunities = []
    seen = set()

    sorted_rows = sorted(
        rows,
        key=lambda row: int(float(row.get("score", 0))) if row.get("score") else 0,
        reverse=True,
    )

    for row in sorted_rows:
        repo = row.get("repo", "")
        if repo and repo not in seen:
            seen.add(repo)
            best_opportunities.append({
                "repo": repo,
                "score": row.get("score", "0"),
                "best_issue": row.get("best_issue", ""),
                "status": row.get("status", DEFAULT_STATUS),
                "progress": STATUS_PROGRESS.get(row.get("status", DEFAULT_STATUS), 12),
                "time": row.get("pretty_time", ""),
                "links": row.get("links", {}),
                "pitch": row.get("pitch", ""),
                "language": row.get("language", "Unknown"),
                "difficulty": row.get("difficulty", "Medium"),
                "merge_probability": row.get("merge_probability", "Medium merge probability"),
                "estimated_time": row.get("estimated_time", "1 day"),
            })

    pipeline_stats = [
        {"status": status, "count": status_counts.get(status, 0)}
        for status in PIPELINE_STATUSES
    ]

    return {
        "rows": rows,
        "total_analyses": total_analyses,
        "unique_repos": unique_repos,
        "unique_visitors": unique_visitors,
        "repeat_visitors": repeat_visitors,
        "average_score": average_score,
        "highest_score": highest_score,
        "top_repos": top_repos,
        "top_issues": issue_counts.most_common(10),
        "daily_activity": daily_activity,
        "best_opportunities": best_opportunities[:5],
        "pipeline_stats": pipeline_stats,
        "pipeline_statuses": PIPELINE_STATUSES,
    }


def generate_pitch(repo, best_issue):
    issue_text = f"issue {best_issue}" if best_issue else "a high-value issue"

    return f"""Hi,

I reviewed {repo} and noticed {issue_text}, which looks like a strong proof-of-work opportunity.

I can investigate it, submit a focused PR, and include a clear technical summary with tests where practical.

If the first contribution is useful, I would be happy to help with a focused 48-hour backend/API reliability sprint.

Best,
Bashir"""
