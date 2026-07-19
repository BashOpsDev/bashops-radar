import json
import threading
import time
from datetime import datetime, timezone

import evidence_service
from evidence_service import InMemoryEvidenceCache, build_evidence_engine


REFERENCE_TIME = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)


def _closed_pull(number, association, created_at, merged_at, labels):
    return {
        "number": number,
        "author_association": association,
        "created_at": created_at,
        "closed_at": merged_at or "2026-07-15T00:00:00Z",
        "merged_at": merged_at,
        "labels": [{"name": label} for label in labels],
        "user": {"login": f"contributor-{number}"},
    }


def _evidence_payloads():
    return {
        "open": [
            {
                "number": number,
                "author_association": "CONTRIBUTOR",
                "created_at": f"2026-07-{number + 1:02d}T00:00:00Z",
                "user": {"login": f"open-{number}"},
            }
            for number in range(1, 10)
        ],
        "closed": [
            _closed_pull(1, "CONTRIBUTOR", "2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z", ["bug"]),
            _closed_pull(2, "FIRST_TIME_CONTRIBUTOR", "2026-07-01T00:00:00Z", "2026-07-04T00:00:00Z", ["bug"]),
            _closed_pull(3, "FIRST_TIMER", "2026-07-01T00:00:00Z", None, ["documentation"]),
            _closed_pull(4, "CONTRIBUTOR", "2026-07-01T00:00:00Z", "2026-07-06T00:00:00Z", ["documentation"]),
            _closed_pull(5, "MEMBER", "2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z", ["internal"]),
            _closed_pull(6, "CONTRIBUTOR", "2026-07-01T00:00:00Z", "2026-07-08T00:00:00Z", ["BUG"]),
        ],
        "releases": [{"published_at": "2026-07-10T00:00:00Z", "draft": False}],
    }


def _repo():
    return {
        "pushed_at": "2026-07-18T00:00:00Z",
        "homepage": "https://example.com",
        "has_sponsors": True,
        "owner": {"type": "Organization"},
    }


def _issue():
    return {
        "number": 42,
        "updated_at": "2026-07-18T00:00:00Z",
        "comments": 3,
        "labels": [{"name": "good first issue"}, {"name": "bug"}],
    }


def test_phase_one_evidence_uses_bounded_historical_samples():
    payloads = _evidence_payloads()
    endpoints = []

    def fetcher(endpoint):
        endpoints.append(endpoint)
        if "state=open" in endpoint:
            return payloads["open"]
        if "state=closed" in endpoint:
            return payloads["closed"]
        if "/releases" in endpoint:
            return payloads["releases"]
        raise AssertionError(endpoint)

    evidence = build_evidence_engine(
        "example/repo",
        _repo(),
        _issue(),
        "Good First Issue",
        fetcher=fetcher,
        reference_time=REFERENCE_TIME,
        cache=InMemoryEvidenceCache(),
    )
    metrics = {metric["key"]: metric for metric in evidence["metrics"]}

    assert len(endpoints) == 3
    assert metrics["contributor_acceptance"]["value"] == "80%"
    assert metrics["contributor_acceptance"]["sample"] == "5 determinable outside-contributor pull requests"
    assert metrics["median_merge_time"]["value"] == "4 days"
    assert metrics["first_time_success"]["value"] == "1 of 2 merged"
    assert metrics["label_intelligence"]["items"][0]["sample"] == 3
    assert metrics["contributor_competition"]["value"] == "Medium"
    assert evidence["completeness"]["percent"] == 83
    assert evidence["historical_support"]["status"] == "Supports the recommendation"
    assert evidence["best_issue"]["reasons"]
    assert "does not alter" in evidence["score_impact"]


def test_evidence_cache_avoids_repeated_github_requests():
    payloads = _evidence_payloads()
    calls = {"count": 0}
    cache = InMemoryEvidenceCache()

    def fetcher(endpoint):
        calls["count"] += 1
        if "state=open" in endpoint:
            return payloads["open"]
        if "state=closed" in endpoint:
            return payloads["closed"]
        return payloads["releases"]

    first = build_evidence_engine(
        "example/repo", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=cache
    )
    second = build_evidence_engine(
        "example/repo", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=cache
    )

    assert first["cache_status"] == "miss"
    assert second["cache_status"] == "hit"
    assert calls["count"] == 3


def test_cold_cache_runs_the_three_bounded_requests_concurrently():
    payloads = _evidence_payloads()
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fetcher(endpoint):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        if "state=open" in endpoint:
            return payloads["open"]
        if "state=closed" in endpoint:
            return payloads["closed"]
        return payloads["releases"]

    build_evidence_engine(
        "example/concurrent",
        _repo(),
        _issue(),
        "Bug Fix",
        fetcher=fetcher,
        reference_time=REFERENCE_TIME,
        cache=InMemoryEvidenceCache(),
    )

    assert max_active == 3


def test_cached_repository_history_recomputes_issue_specific_evidence():
    payloads = _evidence_payloads()
    cache = InMemoryEvidenceCache()

    def fetcher(endpoint):
        if "state=open" in endpoint:
            return payloads["open"]
        if "state=closed" in endpoint:
            return payloads["closed"]
        return payloads["releases"]

    first = build_evidence_engine(
        "example/repo", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=cache
    )
    second_issue = dict(_issue(), number=99, comments=0, labels=[{"name": "documentation"}])
    second = build_evidence_engine(
        "example/repo",
        _repo(),
        second_issue,
        "Docs",
        fetcher=fetcher,
        reference_time=REFERENCE_TIME,
        cache=cache,
    )

    assert first["best_issue"]["issue_number"] == 42
    assert second["best_issue"]["issue_number"] == 99
    assert any("Docs" in reason for reason in second["best_issue"]["reasons"])


def test_missing_historical_endpoints_return_explanations_not_false_precision():
    def unavailable(_endpoint):
        raise RuntimeError("provider failure that must not leak")

    evidence = build_evidence_engine(
        "example/repo",
        _repo(),
        _issue(),
        "Bug Fix",
        fetcher=unavailable,
        reference_time=REFERENCE_TIME,
        cache=InMemoryEvidenceCache(),
    )
    metrics = {metric["key"]: metric for metric in evidence["metrics"]}

    assert evidence["completeness"]["percent"] == 0
    assert evidence["completeness"]["confidence"] == "Low"
    assert metrics["contributor_acceptance"]["value"] == "Not measured"
    assert "could not be collected" in metrics["contributor_acceptance"]["detail"]
    assert "provider failure" not in str(evidence)
    assert evidence["historical_support"]["status"] == "Limited evidence"


def test_no_first_time_contributor_history_is_explained():
    payloads = _evidence_payloads()
    payloads["closed"] = [payloads["closed"][0], payloads["closed"][3]]

    def fetcher(endpoint):
        if "state=open" in endpoint:
            return payloads["open"]
        if "state=closed" in endpoint:
            return payloads["closed"]
        return payloads["releases"]

    evidence = build_evidence_engine(
        "example/no-first-time",
        _repo(),
        _issue(),
        "Bug Fix",
        fetcher=fetcher,
        reference_time=REFERENCE_TIME,
        cache=InMemoryEvidenceCache(),
    )
    first_time = next(metric for metric in evidence["metrics"] if metric["key"] == "first_time_success")

    assert first_time["available"] is False
    assert first_time["value"] == "Not enough history"
    assert "did not mark any sampled" in first_time["detail"]


def test_small_closed_pr_sample_does_not_show_percentage():
    payloads = _evidence_payloads()
    payloads["closed"] = payloads["closed"][:4]

    def fetcher(endpoint):
        if "state=open" in endpoint:
            return payloads["open"]
        if "state=closed" in endpoint:
            return payloads["closed"]
        return payloads["releases"]

    evidence = build_evidence_engine(
        "example/small", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=InMemoryEvidenceCache()
    )
    acceptance = next(metric for metric in evidence["metrics"] if metric["key"] == "contributor_acceptance")

    assert acceptance["available"] is False
    assert acceptance["value"] == "Not enough history"
    assert f"At least {evidence_service.MIN_CLOSED_PR_SAMPLE}" in acceptance["detail"]
    assert "%" not in acceptance["value"]
    closed_history = next(
        check for check in evidence["completeness"]["checks"]
        if check["label"] == "Closed pull-request history"
    )
    contributor_outcomes = next(
        check for check in evidence["completeness"]["checks"]
        if check["label"] == "Contributor outcome history"
    )
    assert closed_history["available"] is True
    assert contributor_outcomes["available"] is False


def test_closed_unmerged_counts_and_closure_time_reconcile():
    payloads = _evidence_payloads()
    payloads["closed"] = [
        _closed_pull(1, "CONTRIBUTOR", "2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z", ["bug"]),
        _closed_pull(2, "CONTRIBUTOR", "2026-07-01T00:00:00Z", "2026-07-04T00:00:00Z", ["bug"]),
        _closed_pull(3, "CONTRIBUTOR", "2026-07-01T00:00:00Z", None, ["bug"]),
        _closed_pull(4, "CONTRIBUTOR", "2026-07-02T00:00:00Z", None, ["Bug"]),
        _closed_pull(5, "CONTRIBUTOR", "2026-07-03T00:00:00Z", None, ["BUG"]),
    ]

    def fetcher(endpoint):
        if "state=open" in endpoint:
            return []
        if "state=closed" in endpoint:
            return payloads["closed"]
        return []

    evidence = build_evidence_engine(
        "example/rejections", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=InMemoryEvidenceCache()
    )
    metrics = {metric["key"]: metric for metric in evidence["metrics"]}

    assert metrics["contributor_acceptance"]["value"] == "40%"
    assert "2 merged; 3 closed without merge" in metrics["contributor_acceptance"]["detail"]
    assert metrics["median_closure_time"]["available"] is True
    assert metrics["label_intelligence"]["items"][0]["sample"] == 5
    assert len(metrics["label_intelligence"]["items"]) == 1


def test_invalid_timestamps_are_excluded_and_explained():
    payloads = _evidence_payloads()
    payloads["closed"][0]["merged_at"] = "2026-06-01T00:00:00Z"
    payloads["closed"][1]["created_at"] = "malformed"

    def fetcher(endpoint):
        if "state=open" in endpoint:
            return payloads["open"]
        if "state=closed" in endpoint:
            return payloads["closed"]
        return payloads["releases"]

    evidence = build_evidence_engine(
        "example/timestamps", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=InMemoryEvidenceCache()
    )
    merge_time = next(metric for metric in evidence["metrics"] if metric["key"] == "median_merge_time")

    assert merge_time["available"] is False
    assert "2 records with missing or impossible timestamps were excluded" in merge_time["detail"]


def test_unknown_authors_bots_and_deleted_users_are_not_counted_as_rejections():
    payloads = _evidence_payloads()
    unknown = _closed_pull(20, "", "2026-07-01T00:00:00Z", None, ["bug"])
    inconsistent = _closed_pull(21, "NOT_A_GITHUB_VALUE", "2026-07-01T00:00:00Z", None, ["bug"])
    bot = _closed_pull(22, "", "2026-07-01T00:00:00Z", None, ["bug"])
    bot["user"] = {"login": "automation[bot]", "type": "Bot"}
    deleted = _closed_pull(23, "CONTRIBUTOR", "2026-07-01T00:00:00Z", "2026-07-03T00:00:00Z", ["bug"])
    deleted["user"] = None
    payloads["closed"].extend([unknown, inconsistent, bot, deleted])

    def fetcher(endpoint):
        if "state=open" in endpoint:
            return payloads["open"]
        if "state=closed" in endpoint:
            return payloads["closed"]
        return payloads["releases"]

    evidence = build_evidence_engine(
        "example/authors", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=InMemoryEvidenceCache()
    )
    metrics = {metric["key"]: metric for metric in evidence["metrics"]}

    assert metrics["contributor_acceptance"]["sample"] == "6 determinable outside-contributor pull requests"
    assert metrics["contributor_acceptance"]["value"] == "83%"
    assert "2 indeterminate-author, 1 bot, and 0 indeterminate-outcome records were excluded" in metrics["contributor_acceptance"]["detail"]
    assert metrics["contributor_competition"]["available"] is True


def test_malformed_records_and_indeterminate_outcomes_are_excluded_safely():
    payloads = _evidence_payloads()
    no_outcome = {
        "number": 90,
        "author_association": "CONTRIBUTOR",
        "created_at": "2026-07-01T00:00:00Z",
        "closed_at": None,
        "merged_at": None,
        "labels": ["not-an-object"],
        "user": "deleted-user-shape",
    }
    payloads["closed"].extend([no_outcome, "not-an-object"])

    def fetcher(endpoint):
        if "state=open" in endpoint:
            return payloads["open"] + [None]
        if "state=closed" in endpoint:
            return payloads["closed"]
        return payloads["releases"]

    evidence = build_evidence_engine(
        "example/malformed", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=InMemoryEvidenceCache()
    )
    acceptance = next(metric for metric in evidence["metrics"] if metric["key"] == "contributor_acceptance")

    assert acceptance["value"] == "80%"
    assert "1 indeterminate-outcome records were excluded" in acceptance["detail"]
    assert len(evidence["collection_warnings"]) == 2
    assert all("malformed records" in warning for warning in evidence["collection_warnings"])


def test_empty_release_history_does_not_make_recent_activity_inactive():
    payloads = _evidence_payloads()

    def fetcher(endpoint):
        if "state=open" in endpoint:
            return payloads["open"]
        if "state=closed" in endpoint:
            return payloads["closed"]
        return []

    evidence = build_evidence_engine(
        "example/no-releases", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=InMemoryEvidenceCache()
    )
    momentum = next(metric for metric in evidence["metrics"] if metric["key"] == "repository_momentum")

    assert momentum["available"] is True
    assert momentum["value"] in {"Growing", "Stable"}
    assert "0 releases" in momentum["detail"]


def test_future_activity_does_not_inflate_momentum_or_show_negative_issue_age():
    payloads = _evidence_payloads()
    payloads["open"] = [
        {
            "number": number,
            "created_at": "2026-08-01T00:00:00Z",
            "user": {"login": f"future-{number}"},
            "author_association": "CONTRIBUTOR",
        }
        for number in range(6)
    ]
    payloads["releases"] = [{"published_at": "2026-08-01T00:00:00Z", "draft": False}]
    issue = dict(_issue(), updated_at="2026-08-01T00:00:00Z")

    def fetcher(endpoint):
        if "state=open" in endpoint:
            return payloads["open"]
        if "state=closed" in endpoint:
            return []
        return payloads["releases"]

    evidence = build_evidence_engine(
        "example/future-dates",
        _repo(),
        issue,
        "Bug Fix",
        fetcher=fetcher,
        reference_time=REFERENCE_TIME,
        cache=InMemoryEvidenceCache(),
    )
    momentum = next(metric for metric in evidence["metrics"] if metric["key"] == "repository_momentum")

    assert momentum["value"] == "Stable"
    assert "0 sampled PRs opened in 30 days" in momentum["detail"]
    assert "0 releases in 90 days" in momentum["detail"]
    assert "Issue updated 0 days ago." in evidence["best_issue"]["reasons"]


def test_empty_open_queue_is_low_competition_and_large_old_queue_is_high():
    payloads = _evidence_payloads()

    def empty_fetcher(endpoint):
        if "state=open" in endpoint:
            return []
        if "state=closed" in endpoint:
            return payloads["closed"]
        return payloads["releases"]

    empty = build_evidence_engine(
        "example/empty-queue", _repo(), _issue(), "Bug Fix", fetcher=empty_fetcher, reference_time=REFERENCE_TIME, cache=InMemoryEvidenceCache()
    )
    empty_competition = next(metric for metric in empty["metrics"] if metric["key"] == "contributor_competition")
    assert empty_competition["value"] == "Low"

    old_open = [
        {
            "number": number,
            "created_at": "2025-01-01T00:00:00Z",
            "user": {"login": f"person-{number}"},
            "author_association": "CONTRIBUTOR",
        }
        for number in range(30)
    ]

    def busy_fetcher(endpoint):
        if "state=open" in endpoint:
            return old_open
        if "state=closed" in endpoint:
            return payloads["closed"]
        return payloads["releases"]

    busy = build_evidence_engine(
        "example/busy", _repo(), _issue(), "Bug Fix", fetcher=busy_fetcher, reference_time=REFERENCE_TIME, cache=InMemoryEvidenceCache()
    )
    busy_competition = next(metric for metric in busy["metrics"] if metric["key"] == "contributor_competition")
    assert busy_competition["value"] == "High"
    assert "Median queue age" in busy_competition["detail"]


def test_stale_complete_evidence_is_used_when_refresh_fails(monkeypatch):
    payloads = _evidence_payloads()
    clock = {"value": 1000.0}
    monkeypatch.setattr(evidence_service.time, "monotonic", lambda: clock["value"])
    monkeypatch.setattr(evidence_service.config, "EVIDENCE_CACHE_TTL_HOURS", 1)
    monkeypatch.setattr(evidence_service.config, "EVIDENCE_CACHE_STALE_HOURS", 4)
    cache = InMemoryEvidenceCache()

    def fetcher(endpoint):
        if "state=open" in endpoint:
            return payloads["open"]
        if "state=closed" in endpoint:
            return payloads["closed"]
        return payloads["releases"]

    first = build_evidence_engine(
        "example/stale", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=cache
    )
    clock["value"] += 3601
    failures = {"count": 0}

    def unavailable(_endpoint):
        failures["count"] += 1
        raise RuntimeError("rate limit provider-secret")

    later = datetime(2026, 7, 19, 14, 0, tzinfo=timezone.utc)
    stale = build_evidence_engine(
        "example/stale", _repo(), _issue(), "Bug Fix", fetcher=unavailable, reference_time=later, cache=cache
    )

    assert first["collected_at"] == stale["collected_at"]
    assert stale["cache_status"] == "stale"
    assert "2 hours ago" in stale["freshness"]
    assert "GitHub was temporarily unavailable" in stale["stale_warning"]
    assert "provider-secret" not in str(stale)
    assert failures["count"] == 3


def test_partial_cache_expires_without_stale_fallback(monkeypatch):
    payloads = _evidence_payloads()
    clock = {"value": 1000.0}
    monkeypatch.setattr(evidence_service.time, "monotonic", lambda: clock["value"])
    monkeypatch.setattr(evidence_service.config, "EVIDENCE_CACHE_TTL_HOURS", 24)
    cache = InMemoryEvidenceCache()
    calls = {"count": 0}

    def partial(endpoint):
        calls["count"] += 1
        if "state=open" in endpoint:
            raise RuntimeError("timeout")
        if "state=closed" in endpoint:
            return payloads["closed"]
        return payloads["releases"]

    first = build_evidence_engine(
        "example/partial", _repo(), _issue(), "Bug Fix", fetcher=partial, reference_time=REFERENCE_TIME, cache=cache
    )
    clock["value"] += 3601
    second = build_evidence_engine(
        "example/partial", _repo(), _issue(), "Bug Fix", fetcher=partial, reference_time=REFERENCE_TIME, cache=cache
    )

    assert first["cache_status"] == "miss"
    assert second["cache_status"] == "miss"
    assert calls["count"] == 6


def test_cache_key_normalization_versioning_eviction_and_copy_isolation(monkeypatch):
    payloads = _evidence_payloads()
    cache = InMemoryEvidenceCache(max_entries=2)
    calls = {"count": 0}

    def fetcher(endpoint):
        calls["count"] += 1
        if "state=open" in endpoint:
            return payloads["open"]
        if "state=closed" in endpoint:
            return payloads["closed"]
        return payloads["releases"]

    first = build_evidence_engine(
        "Example/Repo", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=cache
    )
    first["metrics"][0]["value"] = "mutated"
    same = build_evidence_engine(
        "example/repo", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=cache
    )
    assert same["metrics"][0]["value"] != "mutated"
    assert calls["count"] == 3

    build_evidence_engine("example/two", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=cache)
    build_evidence_engine("example/three", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=cache)
    assert cache.get("1.0:example/repo") is None

    monkeypatch.setattr(evidence_service, "EVIDENCE_VERSION", "2.0")
    changed_version = build_evidence_engine(
        "example/two", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=cache
    )
    assert changed_version["version"] == "2.0"
    assert calls["count"] == 12


def test_large_repository_samples_and_serialized_payload_remain_bounded():
    open_pulls = [
        {
            "number": number,
            "created_at": "2026-07-10T00:00:00Z",
            "author_association": "CONTRIBUTOR",
            "user": {"login": f"open-{number}"},
        }
        for number in range(evidence_service.OPEN_PULL_SAMPLE)
    ]
    closed_pulls = [
        _closed_pull(
            number,
            "CONTRIBUTOR",
            "2026-07-01T00:00:00Z",
            "2026-07-03T00:00:00Z" if number % 2 else None,
            [f"Category-{number % 20}"],
        )
        for number in range(evidence_service.CLOSED_PULL_SAMPLE)
    ]
    releases = [
        {"published_at": "2026-07-01T00:00:00Z", "draft": False}
        for _ in range(evidence_service.RELEASE_SAMPLE)
    ]
    endpoints = []

    def fetcher(endpoint):
        endpoints.append(endpoint)
        if "state=open" in endpoint:
            return open_pulls
        if "state=closed" in endpoint:
            return closed_pulls
        return releases

    evidence = build_evidence_engine(
        "example/large", _repo(), _issue(), "Bug Fix", fetcher=fetcher, reference_time=REFERENCE_TIME, cache=InMemoryEvidenceCache()
    )

    assert len(endpoints) == 3
    assert f"per_page={evidence_service.OPEN_PULL_SAMPLE}" in endpoints[0]
    assert f"per_page={evidence_service.CLOSED_PULL_SAMPLE}" in endpoints[1]
    assert f"per_page={evidence_service.RELEASE_SAMPLE}" in endpoints[2]
    assert all("page=" not in endpoint.replace("per_page=", "") for endpoint in endpoints)
    labels = next(metric for metric in evidence["metrics"] if metric["key"] == "label_intelligence")
    assert len(labels["items"]) <= 5
    assert len(json.dumps(evidence)) < 50_000
