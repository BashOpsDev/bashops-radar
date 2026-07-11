import os
from datetime import datetime, timezone
from collections import Counter, defaultdict

from database import SessionLocal
from models import Target

try:
    import google.generativeai as genai
except Exception:
    genai = None

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY and genai:
    genai.configure(api_key=GEMINI_API_KEY)

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
        "best_issue_url": best_issue_url or (f"{repo_url}/issues" if repo_url else ""),
        "org_url": org_url,
    }


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
        return "2-4h"
    if score >= 60:
        return "1 day"
    return "2 days"


def _pitch_fallback(repo, best_issue):
    """Fast, static template — no network call. Used as (a) the placeholder
    stored at analysis time, and (b) the fallback if Gemini fails/unset."""
    issue_text = f"issue {best_issue}" if best_issue else "a high-value issue"

    return f"""Hi,

I reviewed {repo} and noticed {issue_text}, which looks like a strong proof-of-work opportunity.

I can investigate it, submit a focused PR, and include a clear technical summary with tests where practical.

If the first contribution is useful, I would be happy to help with a focused 48-hour backend/API reliability sprint.

Best,
Bashir"""


def fallback_pitch(repo, best_issue):
    """Public entry point for the free static template (no API call, no
    plan check). This is used as a safe fallback when Pro pitch generation
    cannot reach Gemini."""
    return _pitch_fallback(repo, best_issue)


def generate_pitch(repo, best_issue):
    """
    Generates a founder outreach pitch using Gemini if configured (this is
    the feature sold as "AI Outreach Generator" on the pricing page).
    Falls back to a static template if Gemini fails or isn't configured.

    This is only called on-demand from the "Generate Founder Pitch" button
    (see /generate-pitch in app.py) — NOT at analysis time — so a single
    repo analysis only costs one Gemini call (the repo summary), not two.
    """
    if not GEMINI_API_KEY or not genai:
        return _pitch_fallback(repo, best_issue)

    issue_text = f"issue {best_issue}" if best_issue else "a high-value issue"

    prompt = f"""Write a short, direct founder/maintainer outreach message (under 120 words) from a developer
who is about to contribute to the GitHub repository "{repo}", specifically targeting {issue_text}.

The message should:
1. Reference the repo and the specific issue naturally (not generically)
2. Offer to submit a focused, well-tested PR
3. Softly open the door to a short paid sprint AFTER the PR is useful/merged — do not ask for payment upfront
4. Sound like a real developer message, not a sales email

Sign off as "Bashir". Output only the message text, no preamble."""

    try:
        model = genai.GenerativeModel("gemini-2.5-flash-lite")
        response = model.generate_content(prompt)
        text = response.text.strip() if response and response.text else ""
        return text or _pitch_fallback(repo, best_issue)
    except Exception:
        return _pitch_fallback(repo, best_issue)


def track_analysis(
    repo,
    repo_url,
    score,
    best_issue,
    best_issue_url,
    request,
    user_id=None,
    status=DEFAULT_STATUS,
    language="Unknown",
    stars=0,
    forks=0,
    open_issues=0,
    merge_probability=None,
    difficulty=None,
    estimated_time=None,
):
    """
    Single source of truth write: every analysis becomes one Target row.
    There is no longer a parallel CSV write — analytics.csv / targets.csv
    are not used by the running app.
    """
    ip = request.client.host if request.client else "unknown"

    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    db = SessionLocal()
    try:
        target = Target(
            user_id=user_id,
            repo=repo,
            repo_url=repo_url or "",
            language=language or "Unknown",
            score=float(score or 0),
            status=status,
            best_issue=best_issue or "",
            best_issue_url=best_issue_url or "",
            merge_probability=merge_probability or estimate_merge_probability(score),
            difficulty=difficulty or estimate_difficulty(score),
            estimated_time=estimated_time or estimate_completion_time(score),
            pitch="",
            stars=_int(stars),
            forks=_int(forks),
            open_issues=_int(open_issues),
            ip_address=ip,
        )
        db.add(target)
        db.commit()
    finally:
        db.close()


def daily_analysis_count(user_id=None, ip=None):
    """
    Per-account counter for logged-in users, per-IP fallback for anonymous
    visitors. Replaces the old CSV-scan, which was IP-only and re-read the
    whole file on every request.
    """
    db = SessionLocal()
    try:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        query = db.query(Target).filter(Target.created_at >= today)

        if user_id is not None:
            query = query.filter(Target.user_id == user_id)
        elif ip is not None:
            query = query.filter(Target.user_id.is_(None), Target.ip_address == ip)
        else:
            return 0

        return query.count()
    finally:
        db.close()


def lifetime_analysis_count(user_id):
    """
    Count successful analyses for a user across the lifetime of the account.
    Target rows are created only after analysis succeeds, so this derived
    count avoids a separate mutable quota counter.
    """
    if user_id is None:
        return 0

    db = SessionLocal()
    try:
        return db.query(Target).filter(Target.user_id == user_id).count()
    finally:
        db.close()


def _row_to_dict(row):
    return {
        "id": row.id,
        "repo": row.repo or "",
        "repo_url": row.repo_url or "",
        "score": row.score if row.score is not None else 0,
        "best_issue": row.best_issue or "",
        "status": row.status or DEFAULT_STATUS,
        "language": row.language or "Unknown",
        "stars": row.stars or 0,
        "forks": row.forks or 0,
        "open_issues": row.open_issues or 0,
        "difficulty": row.difficulty or estimate_difficulty(row.score),
        "merge_probability": row.merge_probability or estimate_merge_probability(row.score),
        "estimated_time": row.estimated_time or estimate_completion_time(row.score),
        "pitch": row.pitch or "",
        "progress": STATUS_PROGRESS.get(row.status or DEFAULT_STATUS, 12),
        "pretty_time": row.created_at.strftime("%d %b %Y - %H:%M UTC") if row.created_at else "",
        "timestamp": row.created_at.isoformat() if row.created_at else "",
        "links": repo_links(row.repo_url or "", row.best_issue_url or ""),
    }


def analytics_summary(user_id=None):
    """
    Reads straight from Postgres. If user_id is given, every figure is
    scoped to that account only (this is what makes /dashboard and
    /pipeline private per-user instead of showing the same global feed
    to everyone). user_id=None returns the global view, used only by the
    admin-gated /admin/analytics route.
    """
    db = SessionLocal()
    try:
        query = db.query(Target)
        if user_id is not None:
            query = query.filter(Target.user_id == user_id)

        targets = query.order_by(Target.created_at.desc()).all()
    finally:
        db.close()

    rows = [_row_to_dict(t) for t in targets]

    total_analyses = len(rows)
    unique_repos = len(set(row["repo"] for row in rows if row["repo"]))

    scores = [int(float(row["score"])) for row in rows if row["score"] is not None]
    average_score = round(sum(scores) / len(scores), 1) if scores else 0
    highest_score = max(scores) if scores else 0

    repo_counts = Counter()
    issue_counts = Counter()
    repo_scores = {}
    daily_counts = defaultdict(int)
    status_counts = Counter()

    for row in rows:
        repo_counts[row["repo"] or "unknown"] += 1
        status_counts[row["status"]] += 1

        if row["best_issue"]:
            issue_counts[row["best_issue"]] += 1

        repo_scores[row["repo"]] = int(float(row["score"] or 0))

        if row["timestamp"]:
            try:
                dt = datetime.fromisoformat(row["timestamp"])
                daily_counts[dt.strftime("%d %b")] += 1
            except Exception:
                pass

    top_repos = [
        {"repo": repo, "count": count, "score": repo_scores.get(repo, 0)}
        for repo, count in repo_counts.most_common(10)
    ]

    daily_activity = [
        {"day": day, "count": count, "bar": "#" * min(count, 20)}
        for day, count in list(daily_counts.items())[-7:]
    ]

    best_opportunities = []
    seen = set()
    for row in sorted(rows, key=lambda r: int(float(r["score"] or 0)), reverse=True):
        repo = row["repo"]
        if repo and repo not in seen:
            seen.add(repo)
            best_opportunities.append(row)

    pipeline_stats = [
        {"status": status, "count": status_counts.get(status, 0)}
        for status in PIPELINE_STATUSES
    ]

    return {
        "rows": rows,
        "total_analyses": total_analyses,
        "unique_repos": unique_repos,
        "average_score": average_score,
        "highest_score": highest_score,
        "top_repos": top_repos,
        "top_issues": issue_counts.most_common(10),
        "daily_activity": daily_activity,
        "best_opportunities": best_opportunities[:5],
        "pipeline_stats": pipeline_stats,
        "pipeline_statuses": PIPELINE_STATUSES,
    }


def save_pitch(repo, user_id, pitch):
    """Persists an on-demand generated pitch to every matching row for this
    user so it's not silently regenerated (and re-billed against Gemini
    quota) every time the pipeline page is reloaded."""
    if user_id is None:
        return

    db = SessionLocal()
    try:
        rows = (
            db.query(Target)
            .filter(Target.repo == repo, Target.user_id == user_id)
            .all()
        )
        for row in rows:
            row.pitch = pitch
        db.commit()
    finally:
        db.close()


def update_pipeline_status(repo, status, user_id):
    """
    Always scoped to the user making the request — a user can only ever
    update rows that belong to them.
    """
    if status not in PIPELINE_STATUSES or user_id is None:
        return False

    db = SessionLocal()
    try:
        rows = (
            db.query(Target)
            .filter(Target.repo == repo, Target.user_id == user_id)
            .all()
        )

        if not rows:
            return False

        for row in rows:
            row.status = status

        db.commit()
        return True
    finally:
        db.close()
