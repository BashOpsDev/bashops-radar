import re
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from analysis_service import build_analysis_result, contract_potential
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

JOB_CATEGORY_DEFINITIONS = [
    {
        "key": "trending",
        "title": "Today's Best Opportunities",
        "description": "The strongest current Radar analyses across the bounded public opportunity feed.",
    },
    {
        "key": "paid-sprint",
        "title": "Highest Paid Sprint Potential",
        "description": "Commercial open-source projects showing stronger business, maintenance, and technical-work signals. These are opportunity indicators, not guarantees.",
    },
    {
        "key": "fast-merge",
        "title": "Fast Merge Opportunities",
        "description": "Repositories where recent public evidence suggests active maintenance and a more manageable contribution path. Merge timing is never guaranteed.",
    },
    {
        "key": "founder-friendly",
        "title": "Founder Friendly",
        "description": "Actively maintained projects where visible commercial and contributor signals may make a genuine conversation easier to evaluate. Founder identity is not inferred.",
    },
    {
        "key": "great-first-contribution",
        "title": "Great First Contribution",
        "description": "Clearer, more approachable opportunities with public evidence that an outside contributor can get started.",
    },
    {
        "key": "backend",
        "title": "Backend",
        "description": "Opportunities centred on APIs, databases, authentication, services, workers, and server-side systems.",
    },
    {
        "key": "frontend",
        "title": "Frontend",
        "description": "Opportunities involving interfaces, accessibility, design systems, browser behaviour, and frontend frameworks.",
    },
    {
        "key": "ai",
        "title": "AI",
        "description": "Repositories working on models, agents, evaluation, inference, retrieval, and AI development infrastructure.",
    },
    {
        "key": "devops",
        "title": "DevOps",
        "description": "Opportunities involving CI/CD, containers, deployment, automation, reliability, and developer operations.",
    },
    {
        "key": "infrastructure",
        "title": "Infrastructure",
        "description": "Lower-level systems involving databases, cloud platforms, networking, distributed systems, and developer infrastructure.",
    },
    {
        "key": "python",
        "title": "Python",
        "description": "Current opportunities in repositories where Python is the primary detected language.",
    },
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


def _public_evidence_metric(result: dict, key: str) -> dict:
    for metric in (result.get("evidence") or {}).get("metrics") or []:
        if metric.get("key") == key:
            return {
                "available": bool(metric.get("available")),
                "value": str(metric.get("value") or "Unavailable")[:100],
                "sample": str(metric.get("sample") or "Sample unavailable")[:160],
            }
    return {"available": False, "value": "Unavailable", "sample": "Sample unavailable"}


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
        "score_confidence": (result.get("score_transparency") or {}).get("confidence") or "Unavailable",
        "best_issue_labels": [
            str(label.get("name") or "")[:100]
            for label in (issue_source.get("labels") or [])
            if isinstance(label, dict) and label.get("name")
        ][:10],
        "evidence_metrics": {
            key: _public_evidence_metric(result, key)
            for key in (
                "contributor_acceptance",
                "median_merge_time",
                "contributor_competition",
                "repository_momentum",
            )
        },
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
    item.decision = str(result.get("decision") or "Recommendation unavailable")[:255]
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


def public_opportunity_payload(item: OpportunityFeedItem, reference_time=None) -> dict:
    """Expose the bounded cached summary used by public distribution surfaces."""
    reference_time = reference_time or now_utc()
    expires_at = aware_utc(item.expires_at)
    analyzed_at = aware_utc(item.analyzed_at)
    return {
        "repository": item.repository_full_name,
        "repository_url": item.repository_url,
        "language": item.primary_language or "Unavailable",
        "radar_score": int(round(item.radar_score)),
        "decision": item.decision,
        "best_issue": {
            "number": item.best_issue_number,
            "title": item.best_issue_title,
            "url": item.best_issue_url,
        } if item.best_issue_number else None,
        "difficulty": item.difficulty or "Unavailable",
        "merge_probability": item.merge_probability or "Unavailable",
        "contract_potential": contract_potential(int(round(item.radar_score))),
        "maintainer_activity": item.maintainer_activity_signal or "Unavailable",
        "reason": item.public_reason,
        "categories": list(item.categories or []),
        "topics": list(item.topics or []),
        "analyzed_at": analyzed_at.isoformat() if analyzed_at else None,
        "is_stale": bool(expires_at and expires_at <= reference_time),
    }


def _structured_category_text(item) -> str:
    values = [
        item.primary_language,
        *(item.categories or []),
        *(item.topics or []),
        item.best_issue_title,
    ]
    return " ".join(str(value or "") for value in values).casefold()


def _matches_category_terms(item, *terms: str) -> bool:
    text = _structured_category_text(item)
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(term.casefold())}(?![a-z0-9])", text)
        for term in terms
    )


def _commercial_present(item) -> bool:
    value = (item.commercial_signal or "").casefold()
    return bool(value) and not any(
        marker in value
        for marker in ("unavailable", "not detected", "does not show a commercial signal")
    )


def _source_snapshot(item) -> dict:
    value = item.source_snapshot or {}
    return value if isinstance(value, dict) else {}


def _snapshot_metric(item, key: str) -> dict:
    metrics = _source_snapshot(item).get("evidence_metrics") or {}
    if not isinstance(metrics, dict):
        return {}
    return metrics.get(key) or {}


def _metric_number(metric: dict) -> float | None:
    if not metric.get("available"):
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(metric.get("value") or ""))
    return float(match.group()) if match else None


def _recent_maintenance(item) -> bool:
    activity = (item.maintainer_activity_signal or "").casefold()
    momentum = str(_source_snapshot(item).get("momentum") or "").casefold()
    if "high" in activity or momentum in {"growing", "stable"}:
        return True
    match = re.search(r"pushed\s+(\d+)\s+days?\s+ago", (item.recent_activity_signal or "").casefold())
    return bool(match and int(match.group(1)) <= 30)


def _category_ranking_detail(item, category_key: str) -> dict:
    radar_score = max(0, min(float(item.radar_score or 0), 100))
    relevance = 0
    signals = []
    missing = []

    def add(points: int, condition: bool, reason: str) -> None:
        nonlocal relevance
        if condition:
            relevance += points
            signals.append(reason)

    active = _recent_maintenance(item)
    commercial = _commercial_present(item)
    has_issue = bool(item.best_issue_url and item.best_issue_title)
    difficulty = (item.difficulty or "").casefold()
    merge_estimate = (item.merge_probability or "").casefold()
    acceptance = _metric_number(_snapshot_metric(item, "contributor_acceptance"))
    merge_days = _metric_number(_snapshot_metric(item, "median_merge_time"))
    competition = str(_snapshot_metric(item, "contributor_competition").get("value") or "").casefold()
    issue_labels = [str(value).casefold() for value in _source_snapshot(item).get("best_issue_labels") or []]

    eligible = True
    minimum_relevance = 45
    if category_key == "trending":
        relevance = round(radar_score)
        signals.append(item.public_reason or "The cached Radar analysis contains current public opportunity signals")
        minimum_relevance = 0
    elif category_key == "paid-sprint":
        technical_scope = _matches_category_terms(
            item, "api", "integration", "infrastructure", "reliability", "performance", "security", "database", "backend"
        )
        add(30, commercial, "Visible organization, homepage, or sponsor metadata provides commercial context")
        add(20, technical_scope, "The ranked work matches meaningful operational or product-maintenance scope")
        add(15, active, "Recent public activity indicates ongoing maintenance")
        add(15, (item.paid_sprint_signal or "").startswith("Potential"), "The canonical Radar analysis found adjacent-work signals")
        add(10, has_issue, "A concrete ranked issue is available for proof-of-work")
        add(10, radar_score >= 80, "The independent Radar opportunity score is strong")
        eligible = commercial and relevance >= 55
        if not commercial:
            missing.append("No explicit commercial context was detected")
    elif category_key == "fast-merge":
        if merge_days is not None:
            add(30, merge_days <= 14, f"Sampled median merge time is {merge_days:g} days")
            add(15, 14 < merge_days <= 30, f"Sampled median merge time is {merge_days:g} days")
        if acceptance is not None:
            add(15, acceptance >= 60, f"Sampled contributor acceptance is {acceptance:g}%")
        add(15, merge_estimate == "high", "The issue-level merge estimate is High")
        add(15, difficulty in {"low", "easy"}, "The ranked issue has a Low difficulty estimate")
        add(8, difficulty == "medium", "The ranked issue has a Medium difficulty estimate")
        add(10, competition == "low", "The observed open-PR competition signal is Low")
        add(5, competition == "medium", "The observed open-PR competition signal is Medium")
        add(15, has_issue, "A bounded ranked issue is available")
        add(10, active, "Recent public activity indicates active maintenance")
        eligible = has_issue and relevance >= 45
        if merge_days is None:
            missing.append("Observed merge timing is unavailable; the issue-level value remains an estimate")
    elif category_key == "founder-friendly":
        add(30, active, "Recent public activity indicates current project maintenance")
        add(25, commercial, "Visible organization, homepage, or sponsor metadata provides commercial context")
        add(15, acceptance is not None and acceptance >= 50, "Sampled outside-contributor outcomes are constructive")
        add(10, competition in {"low", "medium"}, "Observed contributor competition is manageable")
        add(10, has_issue, "A concrete public issue provides a specific conversation starting point")
        eligible = active and commercial and relevance >= 55
        missing.append("Founder identity and direct founder engagement are not inferred from repository metadata")
    elif category_key == "great-first-contribution":
        beginner_signal = _matches_category_terms(item, "good first issue", "help wanted", "documentation", "tests", "examples", "setup") or any(
            any(term in label for term in ("good first", "help wanted", "beginner"))
            for label in issue_labels
        )
        add(35, beginner_signal, "The ranked issue has a contributor-oriented type or label")
        add(20, difficulty in {"low", "easy"}, "The ranked issue has a Low difficulty estimate")
        add(10, difficulty == "medium", "The ranked issue has a Medium difficulty estimate")
        add(20, has_issue, "A concrete bounded issue is available")
        add(10, acceptance is not None and acceptance >= 50, "Sampled outside-contributor outcomes are constructive")
        add(10, active, "Recent public activity reduces stale-issue risk")
        eligible = has_issue and relevance >= 45
    else:
        category_terms = {
            "backend": ("backend", "api", "fastapi", "django", "flask", "database", "authentication", "queue", "worker", "server"),
            "frontend": ("frontend", "react", "vue", "svelte", "angular", "accessibility", "design system", "browser", "component"),
            "ai": ("machine learning", "llm", "ai agent", "agents", "inference", "evaluation", "embeddings", "retrieval", "model serving"),
            "devops": ("devops", "ci/cd", "github actions", "docker", "deployment", "release automation", "build system", "observability"),
            "infrastructure": ("infrastructure", "distributed system", "storage", "networking", "cloud platform", "runtime", "compiler", "orchestration", "platform engineering", "kubernetes"),
        }
        if category_key == "python":
            primary_match = (item.primary_language or "").casefold() == "python"
            add(70, primary_match, "Python is the repository's primary detected language")
            eligible = primary_match
        else:
            relevant = _matches_category_terms(item, *category_terms[category_key])
            add(65, relevant, f"Repository topics, issue type, or ranked issue match {category_key} work")
            eligible = relevant
        add(15, has_issue, "A concrete ranked issue is available")
        add(10, active, "The repository has recent public maintenance activity")
        add(10, radar_score >= 80, "The independent Radar opportunity score is strong")
        eligible = eligible and relevance >= minimum_relevance

    relevance = max(0, min(int(relevance), 100))
    category_score = round((relevance * 0.8) + (radar_score * 0.2)) if category_key != "trending" else round(radar_score)
    confidence = "High" if relevance >= 75 and len(signals) >= 3 else "Medium" if relevance >= 50 and len(signals) >= 2 else "Low"
    if not signals:
        signals.append("No category-specific evidence was available")
    reason = ". ".join(signal.rstrip(".") for signal in signals[:2]) + "."
    return {
        "eligible": eligible,
        "category_score": category_score,
        "category_relevance": relevance,
        "confidence": confidence,
        "ranking_reason": reason,
        "evidence_signals": signals[:4],
        "missing_evidence": missing[:3],
    }


def curated_opportunity_sections(items, limit=5) -> list[dict]:
    """Build strict category candidate pools from one cached repository dataset."""
    items = list(items or [])
    if not items:
        return []
    limit = max(1, min(int(limit or 5), 5))
    sections = []
    for definition in JOB_CATEGORY_DEFINITIONS:
        ranked = []
        for item in items:
            detail = _category_ranking_detail(item, definition["key"])
            if detail["eligible"]:
                ranked.append((item, detail))
        ranked.sort(
            key=lambda value: (
                -value[1]["category_score"],
                -value[1]["category_relevance"],
                -float(value[0].radar_score or 0),
                value[0].repository_full_name.casefold(),
            )
        )
        ranked = ranked[:limit]
        sections.append(
            {
                **definition,
                "items": [item for item, _detail in ranked],
                "ranking_details": {
                    item.repository_full_name.casefold(): detail
                    for item, detail in ranked
                },
            }
        )
    return sections


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
