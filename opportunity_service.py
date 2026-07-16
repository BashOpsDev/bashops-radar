import re
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from analysis_service import build_analysis_result
from database import SessionLocal
from discovery_service import DiscoveryError, discover_candidate_repositories
from models import OpportunityFeedItem, Target, UserOpportunityInteraction


FEED_CACHE_HOURS = 24
PUBLIC_RECOMMENDATION_LIMIT = 8
FREE_RECOMMENDATION_LIMIT = 5
PRO_RECOMMENDATION_LIMIT = 12
FREE_SAVED_LIMIT = 5
PRO_SAVED_LIMIT = 50
DISMISS_COOLDOWN_DAYS = 14
MAX_CANDIDATES = 12
MAX_ANALYSES_PER_REFRESH = 8
REFRESH_FAILURE_COOLDOWN_MINUTES = 15
REFRESH_CATEGORY_VALUES = [
    "python-fastapi",
    "ai-infrastructure",
    "devtools",
    "apis-integrations",
]

_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,155}$")
_refresh_lock = threading.Lock()
_last_refresh_attempt_at = None


class OpportunityFeedError(Exception):
    """Safe feed error that can be displayed without exposing internals."""


def now_utc():
    return datetime.now(timezone.utc)


def aware_utc(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def normalize_repository_full_name(value: str) -> str:
    candidate = (value or "").strip().strip("/")
    if candidate.endswith(".git"):
        candidate = candidate[:-4]
    if not _REPOSITORY_RE.fullmatch(candidate):
        raise OpportunityFeedError("That repository identity is not supported.")
    owner, name = candidate.split("/", 1)
    if owner in {".", ".."} or name in {".", ".."}:
        raise OpportunityFeedError("That repository identity is not supported.")
    return f"{owner}/{name}"


def _candidate(full_name: str, source: str) -> dict | None:
    try:
        normalized = normalize_repository_full_name(full_name)
    except OpportunityFeedError:
        return None
    return {
        "repository_full_name": normalized,
        "repository_url": f"https://github.com/{normalized}",
        "source": source,
    }


def _existing_candidates(db, limit: int) -> list[dict]:
    rows = (
        db.query(OpportunityFeedItem)
        .filter(OpportunityFeedItem.is_active.is_(True))
        .order_by(OpportunityFeedItem.radar_score.desc(), OpportunityFeedItem.last_seen_at.desc())
        .limit(limit)
        .all()
    )
    return [value for row in rows if (value := _candidate(row.repository_full_name, "feed_cache"))]


def _target_candidates(db, limit: int) -> list[dict]:
    rows = (
        db.query(Target.repo)
        .filter(Target.repo.isnot(None), Target.repo != "")
        .order_by(Target.score.desc(), Target.created_at.desc())
        .limit(limit * 4)
        .all()
    )
    candidates = []
    seen = set()
    for (full_name,) in rows:
        value = _candidate(full_name, "existing_radar_analysis")
        if not value or value["repository_full_name"].casefold() in seen:
            continue
        seen.add(value["repository_full_name"].casefold())
        candidates.append(value)
        if len(candidates) >= limit:
            break
    return candidates


def build_candidate_pool(db, limit: int = MAX_CANDIDATES) -> list[dict]:
    """Combine bounded existing evidence and discovery without scoring candidates."""
    limit = max(1, min(int(limit or MAX_CANDIDATES), MAX_CANDIDATES))
    combined = _existing_candidates(db, 4) + _target_candidates(db, 4)
    try:
        discovered = discover_candidate_repositories(REFRESH_CATEGORY_VALUES, limit=limit)
    except DiscoveryError:
        discovered = []
    for item in discovered:
        value = _candidate(item.get("repository_full_name"), "github_discovery")
        if value:
            combined.append(value)

    candidates = []
    seen = set()
    for item in combined:
        key = item["repository_full_name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(item)
        if len(candidates) >= limit:
            break
    return candidates


def _intelligence_signal(result: dict, key: str):
    for signal in result.get("repository_intelligence") or []:
        if signal.get("key") == key:
            return signal
    return {}


def _maintenance_signal(result: dict) -> str:
    for signal in (result.get("score_transparency") or {}).get("signals_used") or []:
        if signal.get("label") == "Maintenance Activity":
            return f"Maintenance activity signal: {signal.get('detail') or 'Unavailable'}"
    return "Maintenance activity signal unavailable"


def _best_issue_source(result: dict) -> dict:
    best_issue = result.get("best_issue") or {}
    for _score, _issue_type, issue in result.get("issues") or []:
        if issue.get("number") == best_issue.get("number"):
            return issue
    return {}


def _days_since(value: str, reference_time: datetime) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, (reference_time - parsed.astimezone(timezone.utc)).days)


def _snapshot_changes(previous: dict, current: dict) -> list[str]:
    if not previous:
        return ["New to today's bounded opportunity pool"]
    changes = []
    if previous.get("best_issue_number") != current.get("best_issue_number"):
        changes.append("Best issue changed")
    if abs(float(previous.get("radar_score") or 0) - float(current.get("radar_score") or 0)) >= 5:
        changes.append("Radar score changed materially")
    if abs(int(previous.get("open_issues") or 0) - int(current.get("open_issues") or 0)) >= 10:
        changes.append("Open issue count changed materially")
    if previous.get("last_push") != current.get("last_push"):
        changes.append("Repository received a newer push")
    if previous.get("best_issue_updated_at") != current.get("best_issue_updated_at"):
        changes.append("Best issue activity changed")
    return changes[:3]


def apply_analysis_to_feed_item(item: OpportunityFeedItem, result: dict, analyzed_at: datetime) -> None:
    full_name = normalize_repository_full_name(result.get("repo") or "")
    owner, repository_name = full_name.split("/", 1)
    repo = result.get("repo_data") or {}
    best_issue = result.get("best_issue") or {}
    try:
        best_issue_number = int(best_issue.get("number")) if best_issue.get("number") else None
    except (TypeError, ValueError):
        best_issue_number = None
    issue_source = _best_issue_source(result)
    categories = []
    for value in [best_issue.get("type"), result.get("language")]:
        if value and value not in categories and value != "Unknown":
            categories.append(str(value)[:100])
    topics = [str(value)[:100] for value in (repo.get("topics") or []) if value][:10]
    commercial = _intelligence_signal(result, "commercial")
    friendliness = _intelligence_signal(result, "friendliness")
    momentum = _intelligence_signal(result, "momentum")
    last_push_days = _days_since(result.get("last_push"), analyzed_at)
    recent_activity = (
        f"Repository pushed {last_push_days} days ago"
        if last_push_days is not None
        else "Recent push signal unavailable"
    )
    commercial_present = commercial.get("value") == "Signals present"
    paid_sprint_signal = (
        "Potential paid-sprint signal: commercial metadata and a strong Radar score are present"
        if commercial_present and float(result.get("score") or 0) >= 80
        else "Paid-sprint potential not established from available metadata"
    )
    reasons = (result.get("score_transparency") or {}).get("reasons") or []
    public_reason = (
        reasons[0].get("detail")
        if reasons and reasons[0].get("detail")
        else "Canonical Radar analysis found current public contribution signals."
    )
    current_snapshot = {
        "radar_score": float(result.get("score") or 0),
        "open_issues": int(result.get("open_issues") or 0),
        "last_push": result.get("last_push") or "",
        "best_issue_number": best_issue_number,
        "best_issue_updated_at": issue_source.get("updated_at") or "",
        "friendliness": friendliness.get("value") or "Unavailable",
        "momentum": momentum.get("value") or "Unavailable",
    }
    current_snapshot["changes"] = _snapshot_changes(item.source_snapshot or {}, current_snapshot)

    item.repository_full_name = full_name
    item.repository_url = f"https://github.com/{full_name}"
    item.repository_owner = owner
    item.repository_name = repository_name
    item.description = str(result.get("description") or "")[:1000]
    item.primary_language = str(result.get("language") or "Unknown")[:100]
    item.categories = categories
    item.topics = topics
    item.radar_score = float(result.get("score") or 0)
    item.decision = str(result.get("decision") or "Inspect manually")[:255]
    item.best_issue_number = best_issue_number
    item.best_issue_title = str(best_issue.get("title") or "")[:500] or None
    item.best_issue_url = (
        f"https://github.com/{full_name}/issues/{best_issue_number}"
        if best_issue_number
        else None
    )
    item.difficulty = str(result.get("difficulty") or "Unavailable")[:100]
    item.merge_probability = str(result.get("merge_probability") or "Unavailable")[:100]
    item.maintainer_activity_signal = _maintenance_signal(result)[:255]
    item.recent_activity_signal = recent_activity[:255]
    item.commercial_signal = str(commercial.get("detail") or "Commercial signal unavailable")[:255]
    item.paid_sprint_signal = paid_sprint_signal[:255]
    item.public_reason = str(public_reason)[:500]
    item.source_snapshot = current_snapshot
    item.analyzed_at = analyzed_at
    item.expires_at = analyzed_at + timedelta(hours=FEED_CACHE_HOURS)
    item.first_seen_at = item.first_seen_at or analyzed_at
    item.last_seen_at = analyzed_at
    item.is_active = True


def fresh_feed_exists(db, reference_time=None) -> bool:
    reference_time = reference_time or now_utc()
    return (
        db.query(OpportunityFeedItem)
        .filter(
            OpportunityFeedItem.is_active.is_(True),
            OpportunityFeedItem.expires_at > reference_time,
        )
        .count()
        >= 1
    )


def refresh_opportunity_feed(force: bool = False, reference_time=None) -> dict:
    """Refresh a small global cache; stale rows remain available on failure."""
    global _last_refresh_attempt_at
    reference_time = reference_time or now_utc()
    db = SessionLocal()
    try:
        if not force and fresh_feed_exists(db, reference_time):
            return {"status": "cache_hit", "updated": 0, "failed": 0}
    finally:
        db.close()

    if (
        not force
        and _last_refresh_attempt_at
        and reference_time - _last_refresh_attempt_at < timedelta(minutes=REFRESH_FAILURE_COOLDOWN_MINUTES)
    ):
        return {"status": "cooldown", "updated": 0, "failed": 0}

    if not _refresh_lock.acquire(blocking=False):
        return {"status": "refresh_in_progress", "updated": 0, "failed": 0}
    try:
        _last_refresh_attempt_at = reference_time
        db = SessionLocal()
        try:
            if not force and fresh_feed_exists(db, reference_time):
                return {"status": "cache_hit", "updated": 0, "failed": 0}
            candidates = build_candidate_pool(db)
        finally:
            db.close()

        updated = 0
        failed = 0
        seen_ids = []
        for candidate in candidates[:MAX_ANALYSES_PER_REFRESH]:
            try:
                result = build_analysis_result(candidate["repository_url"])
                db = SessionLocal()
                try:
                    item = (
                        db.query(OpportunityFeedItem)
                        .filter(func.lower(OpportunityFeedItem.repository_full_name) == result["repo"].casefold())
                        .first()
                    )
                    if item is None:
                        item = OpportunityFeedItem(
                            repository_full_name=result["repo"],
                            categories=[],
                            topics=[],
                            source_snapshot={},
                            first_seen_at=reference_time,
                        )
                        db.add(item)
                    apply_analysis_to_feed_item(item, result, reference_time)
                    db.commit()
                    db.refresh(item)
                    seen_ids.append(item.id)
                    updated += 1
                finally:
                    db.close()
            except Exception:
                failed += 1

        db = SessionLocal()
        try:
            if seen_ids:
                db.query(OpportunityFeedItem).filter(
                    OpportunityFeedItem.is_active.is_(True),
                    OpportunityFeedItem.expires_at <= reference_time - timedelta(hours=FEED_CACHE_HOURS),
                    OpportunityFeedItem.id.notin_(seen_ids),
                ).update({"is_active": False}, synchronize_session=False)
                db.commit()
        finally:
            db.close()

        if not updated:
            return {"status": "stale" if failed else "empty", "updated": 0, "failed": failed}
        return {"status": "refreshed", "updated": updated, "failed": failed}
    finally:
        _refresh_lock.release()


def load_feed_items(db, include_stale: bool = True) -> list[OpportunityFeedItem]:
    query = db.query(OpportunityFeedItem).filter(OpportunityFeedItem.is_active.is_(True))
    if not include_stale:
        query = query.filter(OpportunityFeedItem.expires_at > now_utc())
    return query.order_by(OpportunityFeedItem.radar_score.desc(), OpportunityFeedItem.last_seen_at.desc()).all()


def feed_freshness(items: list[OpportunityFeedItem], reference_time=None) -> dict:
    reference_time = reference_time or now_utc()
    latest = max((aware_utc(item.analyzed_at) for item in items if item.analyzed_at), default=None)
    stale = bool(items) and not any(aware_utc(item.expires_at) > reference_time for item in items)
    return {"last_updated": latest, "is_stale": stale}


def profile_match(item: OpportunityFeedItem, profile) -> tuple[int | None, list[str]]:
    if profile is None:
        return None, []
    strengths = profile.strength_data or {}
    languages = {str(value.get("label") or "").casefold() for value in strengths.get("languages") or []}
    categories = {str(value.get("label") or "").casefold() for value in strengths.get("categories") or []}
    repositories = {
        str(record.get("repository") or "").casefold()
        for record in (profile.contribution_data or [])
        if record.get("repository")
    }
    item_language = (item.primary_language or "").casefold()
    item_categories = {str(value).casefold() for value in (item.categories or [])}
    item_topics = {str(value).casefold() for value in (item.topics or [])}
    score = 0
    reasons = []
    if item_language and item_language in languages:
        score += 35
        reasons.append(f"Matches your {item.primary_language} contribution history")
    overlapping_categories = sorted(categories & item_categories)
    if overlapping_categories:
        score += min(30, 15 * len(overlapping_categories))
        reasons.append(f"Aligns with your {overlapping_categories[0].title()} strength")
    topic_matches = sorted((categories | languages) & item_topics)
    if topic_matches:
        score += 15
        reasons.append(f"Repository topic overlap: {topic_matches[0]}")
    if item.repository_full_name.casefold() in repositories:
        score += 20
        reasons.append("Similar to a repository in your public contribution evidence")
    if item.best_issue_url:
        score += 10
        reasons.append("A ranked public issue is available")
    return min(score, 100), reasons[:3]


def ranked_recommendations(items, profile=None, limit=FREE_RECOMMENDATION_LIMIT, dismissed_ids=None):
    dismissed_ids = set(dismissed_ids or [])
    ranked = []
    for item in items:
        if item.id in dismissed_ids:
            continue
        match_score, match_reasons = profile_match(item, profile)
        rank_value = float(item.radar_score or 0) + ((match_score or 0) * 0.45)
        ranked.append(
            {
                "item": item,
                "match_score": match_score,
                "match_reasons": match_reasons,
                "changes": list((item.source_snapshot or {}).get("changes") or []),
                "rank_value": rank_value,
            }
        )
    ranked.sort(key=lambda value: (value["rank_value"], value["item"].radar_score), reverse=True)
    return ranked[: max(1, int(limit or FREE_RECOMMENDATION_LIMIT))]


def interaction_ids(db, user_id: int, action: str, since=None) -> set[int]:
    query = db.query(UserOpportunityInteraction.feed_item_id).filter(
        UserOpportunityInteraction.user_id == user_id,
        UserOpportunityInteraction.action == action,
    )
    if since is not None:
        query = query.filter(UserOpportunityInteraction.updated_at >= since)
    return {feed_item_id for (feed_item_id,) in query.all()}


def upsert_interaction(db, user_id: int, feed_item_id: int, action: str):
    allowed = {"saved", "dismissed", "viewed", "analyzed", "repository_opened", "issue_opened"}
    if action not in allowed:
        raise OpportunityFeedError("That opportunity action is not supported.")
    item = db.query(OpportunityFeedItem).filter(OpportunityFeedItem.id == feed_item_id).first()
    if not item:
        raise OpportunityFeedError("That opportunity is no longer available.")
    interaction = db.query(UserOpportunityInteraction).filter(
        UserOpportunityInteraction.user_id == user_id,
        UserOpportunityInteraction.feed_item_id == feed_item_id,
        UserOpportunityInteraction.action == action,
    ).first()
    if interaction is None:
        interaction = UserOpportunityInteraction(user_id=user_id, feed_item_id=feed_item_id, action=action)
        db.add(interaction)
    interaction.updated_at = now_utc()
    if action in {"repository_opened", "issue_opened", "analyzed"}:
        viewed = db.query(UserOpportunityInteraction).filter(
            UserOpportunityInteraction.user_id == user_id,
            UserOpportunityInteraction.feed_item_id == feed_item_id,
            UserOpportunityInteraction.action == "viewed",
        ).first()
        if viewed is None:
            viewed = UserOpportunityInteraction(user_id=user_id, feed_item_id=feed_item_id, action="viewed")
            db.add(viewed)
        viewed.updated_at = now_utc()
    db.commit()
    return item


def saved_items(db, user_id: int, limit: int) -> list[OpportunityFeedItem]:
    return (
        db.query(OpportunityFeedItem)
        .join(UserOpportunityInteraction, UserOpportunityInteraction.feed_item_id == OpportunityFeedItem.id)
        .filter(
            UserOpportunityInteraction.user_id == user_id,
            UserOpportunityInteraction.action == "saved",
        )
        .order_by(UserOpportunityInteraction.updated_at.desc())
        .limit(limit)
        .all()
    )


def recently_viewed_items(db, user_id: int, limit: int = 5) -> list[OpportunityFeedItem]:
    return (
        db.query(OpportunityFeedItem)
        .join(UserOpportunityInteraction, UserOpportunityInteraction.feed_item_id == OpportunityFeedItem.id)
        .filter(
            UserOpportunityInteraction.user_id == user_id,
            UserOpportunityInteraction.action == "viewed",
        )
        .order_by(UserOpportunityInteraction.updated_at.desc())
        .limit(limit)
        .all()
    )


def can_save_more(db, user_id: int, is_pro: bool) -> bool:
    limit = PRO_SAVED_LIMIT if is_pro else FREE_SAVED_LIMIT
    count = db.query(UserOpportunityInteraction).filter(
        UserOpportunityInteraction.user_id == user_id,
        UserOpportunityInteraction.action == "saved",
    ).count()
    return count < limit
