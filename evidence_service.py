"""Bounded, deterministic historical evidence for Radar repository reports."""

from __future__ import annotations

import copy
import statistics
import threading
import time
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable

import config


EVIDENCE_VERSION = "1.0"
OPEN_PULL_SAMPLE = config.EVIDENCE_OPEN_PR_SAMPLE
CLOSED_PULL_SAMPLE = config.EVIDENCE_CLOSED_PR_SAMPLE
RELEASE_SAMPLE = config.EVIDENCE_RELEASE_SAMPLE
MIN_CLOSED_PR_SAMPLE = config.EVIDENCE_MIN_CLOSED_PR_SAMPLE
MIN_TIMING_SAMPLE = 3
MIN_LABEL_SAMPLE = 3
MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}
FIRST_TIME_ASSOCIATIONS = {"FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR"}
OUTSIDE_CONTRIBUTOR_ASSOCIATIONS = {"CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR", "NONE"}


class EvidenceCache:
    """Small cache boundary that can later be backed by Redis."""

    def get(self, key: str) -> dict | None:
        raise NotImplementedError

    def get_stale(self, key: str) -> dict | None:
        raise NotImplementedError

    def set(self, key: str, value: dict, ttl_seconds: int, stale_seconds: int = 0) -> None:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError


class InMemoryEvidenceCache(EvidenceCache):
    def __init__(self, max_entries: int = 256):
        self.max_entries = max(1, max_entries)
        self._entries: OrderedDict[str, tuple[float, float, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> dict | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return None
            expires_at, stale_until, value = entry
            if expires_at <= now:
                if stale_until <= now:
                    self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return copy.deepcopy(value)

    def get_stale(self, key: str) -> dict | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return None
            expires_at, stale_until, value = entry
            if stale_until <= now:
                self._entries.pop(key, None)
                return None
            if expires_at > now:
                return None
            self._entries.move_to_end(key)
            return copy.deepcopy(value)

    def set(self, key: str, value: dict, ttl_seconds: int, stale_seconds: int = 0) -> None:
        now = time.monotonic()
        expires_at = now + max(1, ttl_seconds)
        with self._lock:
            self._entries[key] = (expires_at, expires_at + max(0, stale_seconds), copy.deepcopy(value))
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_evidence_cache = InMemoryEvidenceCache(config.EVIDENCE_CACHE_MAX_ENTRIES)


def clear_evidence_cache() -> None:
    _evidence_cache.clear()


def _parse_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _freshness_label(collected_at, reference_time: datetime | None = None) -> str:
    collected = _parse_datetime(collected_at)
    if not collected:
        return "Collection time unavailable"
    now = reference_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_seconds = max(0, (now.astimezone(timezone.utc) - collected).total_seconds())
    if age_seconds < 3600:
        return "Less than 1 hour ago"
    hours = round(age_seconds / 3600)
    if hours < 48:
        return f"{hours} hours ago"
    return f"{round(hours / 24)} days ago"


def _median_days(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _observation_window(records: list[dict], fields=("created_at", "closed_at", "merged_at")) -> str:
    dates = [
        parsed
        for record in records
        for field in fields
        if (parsed := _parse_datetime(record.get(field))) is not None
    ]
    if not dates:
        return "Observation dates were not available."
    return f"{min(dates).date().isoformat()} to {max(dates).date().isoformat()}"


def _sample_label(count: int, noun: str) -> str:
    return f"{count} {noun if count == 1 else noun + 's'}"


def _metric(
    key: str,
    label: str,
    value: str,
    detail: str,
    *,
    available: bool,
    source: str,
    sample: str,
    window: str,
    limitation: str,
    items: list[dict] | None = None,
) -> dict:
    return {
        "key": key,
        "label": label,
        "value": value,
        "detail": detail,
        "available": available,
        "source": source,
        "sample": sample,
        "window": window,
        "limitation": limitation,
        "items": items or [],
    }


def _safe_fetch(fetcher: Callable[[str], object], endpoint: str) -> tuple[list[dict], bool, str | None]:
    try:
        payload = fetcher(endpoint)
    except Exception as exc:
        message = str(exc).lower()
        if "rate limit" in message or "429" in message:
            reason = "GitHub rate limiting prevented this evidence request."
        elif "timed out" in message or "timeout" in message:
            reason = "GitHub timed out during this evidence request."
        else:
            reason = "GitHub could not provide this evidence request."
        return [], False, reason
    if not isinstance(payload, list):
        return [], False, "GitHub returned an unexpected response shape for this evidence request."
    valid_records = [record for record in payload if isinstance(record, dict)]
    warning = None
    if len(valid_records) != len(payload):
        warning = f"GitHub returned {len(payload) - len(valid_records)} malformed records; those records were ignored."
    return valid_records, True, warning


def _association(pull: dict) -> str | None:
    value = str(pull.get("author_association") or "").strip().upper()
    known = MAINTAINER_ASSOCIATIONS | OUTSIDE_CONTRIBUTOR_ASSOCIATIONS
    return value if value in known else None


def _is_bot_pull(pull: dict) -> bool:
    user = pull.get("user") if isinstance(pull.get("user"), dict) else {}
    login = str(user.get("login") or "").lower()
    return str(user.get("type") or "").lower() == "bot" or login.endswith("[bot]")


def _outside_contributor_pulls(pulls: list[dict]) -> list[dict]:
    return [
        pull
        for pull in pulls
        if _association(pull) in OUTSIDE_CONTRIBUTOR_ASSOCIATIONS and not _is_bot_pull(pull)
    ]


def _duration_values(records: list[dict], end_field: str) -> tuple[list[float], int]:
    values = []
    discarded = 0
    for record in records:
        created_at = _parse_datetime(record.get("created_at"))
        ended_at = _parse_datetime(record.get(end_field))
        if not created_at or not ended_at or ended_at < created_at:
            discarded += 1
            continue
        values.append((ended_at - created_at).total_seconds() / 86400)
    return values, discarded


def _timing_metric(key: str, label: str, records: list[dict], end_field: str, window: str, limitation: str) -> dict:
    durations, discarded = _duration_values(records, end_field)
    median_days = _median_days(durations) if len(durations) >= MIN_TIMING_SAMPLE else None
    if median_days is not None:
        value = f"{median_days:g} days"
        detail = f"Median time across {len(durations)} sampled pull requests."
    else:
        value = "Not enough timing history"
        detail = f"At least {MIN_TIMING_SAMPLE} valid pull-request durations are required; {len(durations)} were available."
    if discarded:
        detail += f" {discarded} records with missing or impossible timestamps were excluded."
    return _metric(
        key,
        label,
        value,
        detail,
        available=median_days is not None,
        source=f"Pull-request created_at and {end_field} timestamps",
        sample=_sample_label(len(durations), "valid pull-request duration"),
        window=window,
        limitation=limitation,
    )


def _acceptance_metrics(closed_pulls: list[dict], available: bool) -> tuple[dict, dict, dict, dict]:
    outside = _outside_contributor_pulls(closed_pulls)
    external = [pull for pull in outside if pull.get("closed_at") or pull.get("merged_at")]
    excluded_bots = sum(_is_bot_pull(pull) for pull in closed_pulls)
    excluded_unknown = sum(
        not _is_bot_pull(pull) and _association(pull) is None
        for pull in closed_pulls
    )
    excluded_outcomes = len(outside) - len(external)
    source = "Most recently updated closed pull requests from GitHub"
    limitation = (
        "This is a bounded sample, not the repository's complete pull-request history. "
        "Maintainer-authored, bot-authored, and indeterminate-author records are excluded."
    )
    window = _observation_window(external)

    if not available:
        reason = "GitHub closed pull-request history could not be collected."
        unavailable = _metric(
            "contributor_acceptance",
            "Sampled Contributor Acceptance",
            "Not measured",
            reason,
            available=False,
            source=source,
            sample="0 pull requests",
            window="No observation window",
            limitation=limitation,
        )
        merge_time = dict(unavailable, key="median_merge_time", label="Median Merge Time")
        closure_time = dict(unavailable, key="median_closure_time", label="Median Closure Time Without Merge")
        first_time = dict(unavailable, key="first_time_success", label="First-Time Contributor Outcomes")
        return unavailable, merge_time, closure_time, first_time

    merged = [pull for pull in external if pull.get("merged_at")]
    closed_without_merge = [pull for pull in external if not pull.get("merged_at") and pull.get("closed_at")]
    exclusion_detail = ""
    if excluded_unknown or excluded_bots or excluded_outcomes:
        exclusion_detail = (
            f" {excluded_unknown} indeterminate-author, {excluded_bots} bot, and "
            f"{excluded_outcomes} indeterminate-outcome records were excluded."
        )

    if len(external) < MIN_CLOSED_PR_SAMPLE:
        acceptance = _metric(
            "contributor_acceptance",
            "Sampled Contributor Acceptance",
            "Not enough history",
            f"At least {MIN_CLOSED_PR_SAMPLE} determinable closed outside-contributor pull requests are required; {len(external)} were available.{exclusion_detail}",
            available=False,
            source=source,
            sample=_sample_label(len(external), "determinable outside-contributor pull request"),
            window=window,
            limitation=limitation,
        )
    else:
        rate = round((len(merged) / len(external)) * 100)
        acceptance = _metric(
            "contributor_acceptance",
            "Sampled Contributor Acceptance",
            f"{rate}%",
            f"{len(merged)} merged; {len(closed_without_merge)} closed without merge.{exclusion_detail}",
            available=True,
            source=source,
            sample=_sample_label(len(external), "determinable outside-contributor pull request"),
            window=window,
            limitation=limitation,
        )

    merge_time = _timing_metric("median_merge_time", "Median Merge Time", merged, "merged_at", window, limitation)
    closure_time = _timing_metric(
        "median_closure_time",
        "Median Closure Time Without Merge",
        closed_without_merge,
        "closed_at",
        window,
        limitation,
    )

    first_time_pulls = [
        pull
        for pull in external
        if str(pull.get("author_association") or "").upper() in FIRST_TIME_ASSOCIATIONS
    ]
    first_time_merged = sum(bool(pull.get("merged_at")) for pull in first_time_pulls)
    first_time = _metric(
        "first_time_success",
        "First-Time Contributor Outcomes",
        f"{first_time_merged} of {len(first_time_pulls)} merged" if first_time_pulls else "Not enough history",
        (
            f"{first_time_merged} merged; {len(first_time_pulls) - first_time_merged} closed without merge."
            if first_time_pulls
            else "GitHub did not mark any sampled pull requests as first-time contributor submissions."
        ),
        available=bool(first_time_pulls),
        source="GitHub author association on sampled closed pull requests",
        sample=_sample_label(len(first_time_pulls), "GitHub-marked first-time pull request"),
        window=window,
        limitation=(
            "GitHub's author-association marker and this bounded sample do not represent all historical first contributions. "
            "Missing or unknown associations are excluded rather than treated as unsuccessful submissions."
        ),
    )
    return acceptance, merge_time, closure_time, first_time


def _label_metric(closed_pulls: list[dict], available: bool) -> dict:
    external = [
        pull
        for pull in _outside_contributor_pulls(closed_pulls)
        if pull.get("closed_at") or pull.get("merged_at")
    ]
    stats: dict[str, dict] = defaultdict(lambda: {"display": "", "total": 0, "merged": 0, "merge_days": []})
    for pull in external:
        created_at = _parse_datetime(pull.get("created_at"))
        merged_at = _parse_datetime(pull.get("merged_at"))
        for label in pull.get("labels") or []:
            if not isinstance(label, dict):
                continue
            name = str(label.get("name") or "").strip()
            if not name:
                continue
            normalized_name = name.casefold()
            stats[normalized_name]["display"] = stats[normalized_name]["display"] or name
            stats[normalized_name]["total"] += 1
            if merged_at:
                stats[normalized_name]["merged"] += 1
                if created_at and merged_at >= created_at:
                    stats[normalized_name]["merge_days"].append((merged_at - created_at).total_seconds() / 86400)

    items = []
    for _normalized_name, values in stats.items():
        if values["total"] < MIN_LABEL_SAMPLE:
            continue
        items.append(
            {
                "label": values["display"],
                "sample": values["total"],
                "merged": values["merged"],
                "acceptance_rate": round((values["merged"] / values["total"]) * 100),
                "median_merge_days": _median_days(values["merge_days"]),
            }
        )
    items.sort(key=lambda item: (-item["sample"], -item["acceptance_rate"], item["label"].lower()))
    items = items[:5]

    if not available:
        reason = "GitHub closed pull-request history could not be collected."
    elif not items:
        reason = f"No label appeared on at least {MIN_LABEL_SAMPLE} outside-contributor pull requests in the sample."
    else:
        reason = f"{len(items)} labels had enough sampled pull requests for comparison."
    return _metric(
        "label_intelligence",
        "Pull-Request Label History",
        "Observed" if items else "Not enough label history",
        reason,
        available=bool(items),
        source="Labels and outcomes on sampled closed outside-contributor pull requests",
        sample=_sample_label(len(external), "outside-contributor pull request"),
        window=_observation_window(external),
        limitation=f"Labels are matched case-insensitively, require at least {MIN_LABEL_SAMPLE} sampled PRs, and may still be incomplete or applied inconsistently.",
        items=items,
    )


def _momentum_metric(
    repo: dict,
    open_pulls: list[dict],
    closed_pulls: list[dict],
    releases: list[dict],
    now: datetime,
    *,
    pull_history_available: bool,
    release_history_available: bool,
) -> dict:
    all_pulls = open_pulls + closed_pulls
    current_start = now - timedelta(days=30)
    previous_start = now - timedelta(days=60)
    current = 0
    previous = 0
    for pull in all_pulls:
        created_at = _parse_datetime(pull.get("created_at"))
        if not created_at or created_at > now:
            continue
        if created_at >= current_start:
            current += 1
        elif created_at >= previous_start:
            previous += 1
    recent_releases = sum(
        1
        for release in releases
        if (published := _parse_datetime(release.get("published_at") or release.get("created_at")))
        and now - timedelta(days=90) <= published <= now
        and not release.get("draft")
    )
    pushed_at = _parse_datetime(repo.get("pushed_at"))
    pushed_days = (now - pushed_at).days if pushed_at else None

    history_available = pull_history_available and release_history_available
    if not history_available:
        value = "Not enough activity history"
        available = False
    elif not all_pulls and not releases and pushed_days is None:
        value = "Not enough activity history"
        available = False
    elif current >= 4 and current > previous * 1.25:
        value = "Growing"
        available = True
    elif pushed_days is not None and pushed_days > 90 and current == 0 and recent_releases == 0:
        value = "Slowing"
        available = True
    else:
        value = "Stable"
        available = True
    return _metric(
        "repository_momentum",
        "Observed Development Momentum",
        value,
        (
            f"{current} sampled PRs opened in 30 days; {previous} in the prior 30 days; {recent_releases} releases in 90 days."
            if history_available
            else "Pull-request or release history could not be collected for a trend comparison."
        ),
        available=available,
        source="Bounded open/closed pull-request samples, releases, and repository push time",
        sample=f"{len(all_pulls)} pull requests and {len(releases)} releases",
        window=f"Recent 60-day pull-request comparison as of {now.date().isoformat()}",
        limitation="Samples are sorted by recent updates, so this is an activity indicator rather than a complete growth series.",
    )


def _competition_metric(open_pulls: list[dict], closed_pulls: list[dict], available: bool, now: datetime) -> dict:
    ages = []
    authors = set()
    sampled_pulls = open_pulls + closed_pulls
    for pull in sampled_pulls:
        if _is_bot_pull(pull):
            continue
        user = pull.get("user") if isinstance(pull.get("user"), dict) else {}
        if login := user.get("login"):
            authors.add(str(login).lower())
    for pull in open_pulls:
        if created_at := _parse_datetime(pull.get("created_at")):
            ages.append(max(0, (now - created_at).days))
    median_age = round(statistics.median(ages)) if ages else None
    if not available:
        value = "Not measured"
        detail = "GitHub's open pull-request queue could not be collected."
    elif len(open_pulls) >= 25:
        value = "High"
        detail = f"{len(open_pulls)} sampled open PRs; {len(authors)} distinct non-bot contributors across open and closed samples."
    elif len(open_pulls) >= 8:
        value = "Medium"
        detail = f"{len(open_pulls)} sampled open PRs; {len(authors)} distinct non-bot contributors across open and closed samples."
    else:
        value = "Low"
        detail = f"{len(open_pulls)} sampled open PRs; {len(authors)} distinct non-bot contributors across open and closed samples."
    if median_age is not None:
        detail += f" Median queue age: {median_age} days."
    return _metric(
        "contributor_competition",
        "Contributor Competition",
        value,
        detail,
        available=available,
        source="Current bounded open pull-request queue and deduplicated non-bot authors across open and closed samples",
        sample=f"{len(open_pulls)} open and {len(closed_pulls)} closed pull requests",
        window=f"Queue observed {now.date().isoformat()}",
        limitation="Open queue size is not the same as maintainer review backlog or contributor demand.",
    )


def _commercial_metric(repo: dict, now: datetime) -> dict:
    signals = []
    owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
    if str(owner.get("type") or "").lower() == "organization":
        signals.append("organization-owned")
    if repo.get("homepage"):
        signals.append("project homepage")
    if repo.get("has_sponsors"):
        signals.append("GitHub Sponsors enabled")
    return _metric(
        "commercial_evidence",
        "Explicit Commercial Context",
        "Signals present" if signals else "Not detected",
        ", ".join(signals) if signals else "No explicit commercial signal was present in the repository metadata requested.",
        available=True,
        source="GitHub repository owner type, homepage, and Sponsors metadata",
        sample="1 repository metadata record",
        window=f"Observed {now.date().isoformat()}",
        limitation="These metadata signals do not prove hiring intent, paid work, budget, or contract availability.",
    )


def _best_issue_evidence(issue: dict | None, issue_type: str | None, now: datetime) -> dict:
    if not issue:
        return {
            "available": False,
            "issue_number": None,
            "reasons": [],
            "detail": "No ranked issue was available, so issue-specific historical evidence could not be shown.",
        }
    reasons = []
    labels = [
        str(label.get("name") or "").strip()
        for label in issue.get("labels") or []
        if isinstance(label, dict)
    ]
    contributor_labels = [label for label in labels if any(term in label.lower() for term in ("good first", "help wanted", "beginner"))]
    if contributor_labels:
        reasons.append(f"Contributor-oriented label: {', '.join(contributor_labels[:2])}.")
    updated_at = _parse_datetime(issue.get("updated_at"))
    if updated_at:
        reasons.append(f"Issue updated {max(0, (now - updated_at).days)} days ago.")
    try:
        comments = max(0, int(issue.get("comments") or 0))
    except (TypeError, ValueError):
        comments = 0
    if comments:
        reasons.append(f"{comments} public issue comments indicate visible discussion.")
    if issue_type:
        reasons.append(f"Issue classification from title and labels: {issue_type}.")
    return {
        "available": bool(reasons),
        "issue_number": issue.get("number"),
        "reasons": reasons,
        "detail": "Reasons use only the sampled issue metadata; maintainer response and implementation complexity were not measured.",
    }


def _completeness(
    closed_history_available: bool,
    open_available: bool,
    release_available: bool,
    metrics: list[dict],
) -> dict:
    labels_available = next(metric["available"] for metric in metrics if metric["key"] == "label_intelligence")
    acceptance_available = next(metric["available"] for metric in metrics if metric["key"] == "contributor_acceptance")
    checks = [
        {"label": "Closed pull-request history", "available": closed_history_available},
        {"label": "Open pull-request queue", "available": open_available},
        {"label": "Label outcome history", "available": labels_available},
        {"label": "Release history", "available": release_available},
        {"label": "Contributor outcome history", "available": acceptance_available},
        {"label": "Formal review and response history", "available": False},
    ]
    percent = round((sum(item["available"] for item in checks) / len(checks)) * 100)
    confidence = "High" if percent >= 67 else "Medium" if percent >= 50 else "Low"
    return {
        "percent": percent,
        "confidence": confidence,
        "checks": checks,
        "detail": "Completeness measures which evidence families were available. It does not measure opportunity quality.",
    }


def _historical_support(metrics: list[dict], completeness: dict) -> dict:
    acceptance = next(metric for metric in metrics if metric["key"] == "contributor_acceptance")
    merge_time = next(metric for metric in metrics if metric["key"] == "median_merge_time")
    competition = next(metric for metric in metrics if metric["key"] == "contributor_competition")
    reasons = []
    positive = 0
    negative = 0
    if acceptance["available"]:
        rate = int(acceptance["value"].rstrip("%"))
        reasons.append(f"Sampled contributor acceptance: {rate}%.")
        positive += rate >= 60
        negative += rate < 35
    if merge_time["available"]:
        days = float(merge_time["value"].split()[0])
        reasons.append(f"Sampled median merge time: {days:g} days.")
        positive += days <= 14
        negative += days > 45
    if competition["available"]:
        reasons.append(f"Observed contributor competition: {competition['value'].lower()}.")
        negative += competition["value"] == "High"
    if completeness["percent"] < 50:
        status = "Limited evidence"
    elif positive >= 2 and negative == 0:
        status = "Supports the recommendation"
    elif negative:
        status = "Mixed evidence"
    else:
        status = "Inconclusive evidence"
    return {"status": status, "reasons": reasons or ["Not enough historical evidence was measured."]}


def unavailable_evidence(repository_full_name: str, reason: str) -> dict:
    return {
        "version": EVIDENCE_VERSION,
        "repository": repository_full_name,
        "collected_at": None,
        "freshness": "Historical evidence was not refreshed",
        "cache_status": "unavailable",
        "score_impact": "Informational only; historical evidence does not alter the Opportunity Score.",
        "completeness": {
            "percent": 0,
            "confidence": "Low",
            "checks": [],
            "detail": reason,
        },
        "historical_support": {"status": "Limited evidence", "reasons": [reason]},
        "stale_warning": None,
        "collection_warnings": [reason],
        "metrics": [],
        "best_issue": {"available": False, "issue_number": None, "reasons": [], "detail": reason},
        "gaps": [{"label": "Historical GitHub evidence", "reason": reason}],
    }


def build_evidence_engine(
    repository_full_name: str,
    repo: dict,
    issue: dict | None,
    issue_type: str | None,
    *,
    fetcher: Callable[[str], object],
    reference_time: datetime | None = None,
    cache: EvidenceCache | None = None,
) -> dict:
    """Collect Phase 1 evidence without changing Radar scoring or recommendations."""
    cache = cache or _evidence_cache
    key = f"{EVIDENCE_VERSION}:{repository_full_name.strip().lower()}"
    if cached := cache.get(key):
        cached["cache_status"] = "hit"
        cached["freshness"] = _freshness_label(cached.get("collected_at"), reference_time)
        issue_reference_time = reference_time or datetime.now(timezone.utc)
        cached["best_issue"] = _best_issue_evidence(issue, issue_type, issue_reference_time)
        return cached

    stale_cached = cache.get_stale(key)

    now = reference_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    endpoints = (
        f"/repos/{repository_full_name}/pulls?state=open&sort=updated&direction=desc&per_page={OPEN_PULL_SAMPLE}",
        f"/repos/{repository_full_name}/pulls?state=closed&sort=updated&direction=desc&per_page={CLOSED_PULL_SAMPLE}",
        f"/repos/{repository_full_name}/releases?per_page={RELEASE_SAMPLE}",
    )
    # These reads are independent; running them together keeps a cold-cache
    # failure bounded by one GitHub timeout while preserving the three-call cap.
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="radar-evidence") as executor:
        open_result, closed_result, release_result = executor.map(
            lambda endpoint: _safe_fetch(fetcher, endpoint),
            endpoints,
        )
    open_pulls, open_available, open_error = open_result
    closed_pulls, closed_available, closed_error = closed_result
    releases, release_available, release_error = release_result

    collection_warnings = [error for error in (open_error, closed_error, release_error) if error]
    if stale_cached and collection_warnings:
        stale_cached["cache_status"] = "stale"
        stale_cached["freshness"] = _freshness_label(stale_cached.get("collected_at"), now)
        stale_cached["stale_warning"] = (
            f"Historical evidence is {stale_cached['freshness']}. GitHub was temporarily unavailable during refresh."
        )
        stale_cached["collection_warnings"] = collection_warnings
        stale_cached["best_issue"] = _best_issue_evidence(issue, issue_type, now)
        return stale_cached

    acceptance, merge_time, closure_time, first_time = _acceptance_metrics(closed_pulls, closed_available)
    label_history = _label_metric(closed_pulls, closed_available)
    momentum = _momentum_metric(
        repo,
        open_pulls,
        closed_pulls,
        releases,
        now,
        pull_history_available=open_available or closed_available,
        release_history_available=release_available,
    )
    competition = _competition_metric(open_pulls, closed_pulls, open_available, now)
    commercial = _commercial_metric(repo, now)
    metrics = [acceptance, merge_time, closure_time, first_time, label_history, momentum, competition, commercial]
    usable_closed_history = closed_available and any(
        pull.get("merged_at") or pull.get("closed_at")
        for pull in _outside_contributor_pulls(closed_pulls)
    )
    completeness = _completeness(usable_closed_history, open_available, release_available, metrics)
    evidence = {
        "version": EVIDENCE_VERSION,
        "repository": repository_full_name,
        "collected_at": now.isoformat(),
        "freshness": _freshness_label(now, now),
        "cache_status": "miss",
        "score_impact": "Informational only; historical evidence does not alter the Opportunity Score.",
        "completeness": completeness,
        "historical_support": _historical_support(metrics, completeness),
        "stale_warning": None,
        "collection_warnings": collection_warnings,
        "metrics": metrics,
        "best_issue": _best_issue_evidence(issue, issue_type, now),
        "gaps": [
            {
                "label": "Maintainer responsiveness",
                "reason": "Not measured in Phase 1 because reliable response timing requires bounded comment or timeline history per issue.",
            },
            {
                "label": "Review difficulty and PR size",
                "reason": "Not measured in Phase 1 because formal reviews and changed-file totals require per-PR enrichment or GraphQL.",
            },
            {
                "label": "Maintainer diversity",
                "reason": "Public repository metadata does not provide a reliable maintainer roster or bus-factor measurement.",
            },
        ],
    }
    ttl_seconds = config.EVIDENCE_CACHE_TTL_HOURS * 3600
    complete_collection = open_available and closed_available and release_available and not collection_warnings
    if not complete_collection:
        ttl_seconds = min(ttl_seconds, 3600)
    stale_seconds = config.EVIDENCE_CACHE_STALE_HOURS * 3600 if complete_collection else 0
    cache.set(key, evidence, ttl_seconds, stale_seconds)
    return evidence
