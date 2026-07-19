from types import SimpleNamespace

from opportunity_service import curated_opportunity_sections


def _item(
    repository,
    *,
    score=82,
    language="Go",
    categories=None,
    topics=None,
    issue_title="Fix bounded validation bug",
    difficulty="Medium",
    merge_probability="Medium",
    commercial=False,
    active=True,
    evidence=None,
    labels=None,
):
    return SimpleNamespace(
        repository_full_name=repository,
        repository_url=f"https://github.com/{repository}",
        primary_language=language,
        categories=list(categories or []),
        topics=list(topics or []),
        radar_score=score,
        best_issue_url=f"https://github.com/{repository}/issues/7" if issue_title else None,
        best_issue_title=issue_title,
        difficulty=difficulty,
        merge_probability=merge_probability,
        maintainer_activity_signal="Maintenance activity signal: High" if active else "Maintenance activity signal unavailable",
        recent_activity_signal="Repository pushed 2 days ago" if active else "Recent push signal unavailable",
        commercial_signal=(
            "Organization-owned metadata and project homepage are visible."
            if commercial
            else "Repository metadata alone does not show a commercial signal."
        ),
        paid_sprint_signal=(
            "Potential paid-sprint signal: commercial metadata and a strong Radar score are present"
            if commercial
            else "Paid-sprint potential not established from available metadata"
        ),
        public_reason="Recent activity and a ranked issue are available.",
        source_snapshot={
            "momentum": "Stable" if active else "Unavailable",
            "best_issue_labels": list(labels or []),
            "evidence_metrics": evidence or {},
        },
    )


def _sections(items):
    return {section["key"]: section for section in curated_opportunity_sections(items)}


def _names(section):
    return [item.repository_full_name for item in section["items"]]


def test_every_jobs_category_requires_relevant_public_signals():
    common_evidence = {
        "contributor_acceptance": {"available": True, "value": "72%", "sample": "25 PRs"},
        "median_merge_time": {"available": True, "value": "8 days", "sample": "12 PRs"},
        "contributor_competition": {"available": True, "value": "Low", "sample": "8 open PRs"},
    }
    items = [
        _item("company/platform", categories=["Backend APIs"], commercial=True, evidence=common_evidence),
        _item("community/quick-fixes", categories=["Bug Fix"], difficulty="Low", merge_probability="High", evidence=common_evidence),
        _item("small-team/tool", categories=["DevTools"], commercial=True, evidence=common_evidence),
        _item("community/onboarding", categories=["Good First Issue"], difficulty="Low", labels=["good first issue"], evidence=common_evidence),
        _item("services/backend", categories=["Backend APIs"], topics=["database", "authentication"]),
        _item("web/frontend", language="TypeScript", categories=["Frontend"], topics=["react", "accessibility"]),
        _item("models/engine", language="Python", topics=["llm", "inference", "evaluation"]),
        _item("delivery/automation", topics=["github actions", "docker", "deployment"]),
        _item("systems/runtime", language="Rust", topics=["distributed system", "storage", "runtime"]),
        _item("python/library", language="Python", issue_title="Correct parser validation"),
    ]
    sections = _sections(items)

    assert "company/platform" in _names(sections["paid-sprint"])
    assert "community/quick-fixes" in _names(sections["fast-merge"])
    assert "small-team/tool" in _names(sections["founder-friendly"])
    assert "community/onboarding" in _names(sections["great-first-contribution"])
    assert "services/backend" in _names(sections["backend"])
    assert "web/frontend" in _names(sections["frontend"])
    assert "models/engine" in _names(sections["ai"])
    assert "delivery/automation" in _names(sections["devops"])
    assert "systems/runtime" in _names(sections["infrastructure"])
    assert "python/library" in _names(sections["python"])


def test_category_relevance_outranks_global_score_and_reasons_are_category_specific():
    strong = _item(
        "company/operations",
        score=81,
        categories=["Backend APIs", "Infrastructure"],
        topics=["database", "reliability"],
        commercial=True,
    )
    weak = _item("company/high-score", score=97, issue_title=None, commercial=True, active=False)
    sections = _sections([weak, strong])

    assert _names(sections["paid-sprint"])[0] == "company/operations"
    paid_detail = sections["paid-sprint"]["ranking_details"]["company/operations"]
    backend_detail = sections["backend"]["ranking_details"]["company/operations"]
    assert paid_detail["ranking_reason"] != backend_detail["ranking_reason"]
    assert paid_detail["category_relevance"] >= 55
    assert backend_detail["category_relevance"] >= 45


def test_fast_merge_prefers_observed_history_and_rankings_are_deterministic():
    observed = _item(
        "community/observed",
        score=78,
        difficulty="Low",
        merge_probability="Medium",
        evidence={
            "contributor_acceptance": {"available": True, "value": "80%", "sample": "30 PRs"},
            "median_merge_time": {"available": True, "value": "6 days", "sample": "15 PRs"},
            "contributor_competition": {"available": True, "value": "Low", "sample": "5 open PRs"},
        },
    )
    estimated = _item("community/estimated", score=94, difficulty="Low", merge_probability="High")

    first = _sections([estimated, observed])
    second = _sections([estimated, observed])
    assert _names(first["fast-merge"])[0] == "community/observed"
    assert _names(first["fast-merge"]) == _names(second["fast-merge"])


def test_irrelevant_repositories_are_not_used_as_category_filler():
    unrelated = _item(
        "personal/demo",
        score=99,
        language="C",
        categories=["Experiment"],
        topics=["demo"],
        issue_title=None,
        active=False,
    )
    sections = _sections([unrelated])

    assert _names(sections["trending"]) == ["personal/demo"]
    for key, section in sections.items():
        if key != "trending":
            assert section["items"] == []
            assert section["description"]
