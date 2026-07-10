"""
Unit tests for the pure functions in radar.py — no network calls, no
database, no fixtures needed. These are the scoring/classification
functions that decide what a user sees as an "opportunity score," so
they're the highest-value thing in the codebase to have regression tests
for.
"""

from datetime import datetime, timedelta, timezone

import pytest

from radar import (
    classify_issue,
    days_since,
    decision,
    estimate_difficulty,
    parse_github_url,
    primary_language,
    recommend_angle,
    score_issue,
    score_repo,
    score_repo_signal_report,
)


# --- parse_github_url -------------------------------------------------

def test_parse_github_url_valid():
    assert parse_github_url("https://github.com/octocat/hello-world") == (
        "octocat",
        "hello-world",
    )


def test_parse_github_url_trailing_slash():
    assert parse_github_url("https://github.com/octocat/hello-world/") == (
        "octocat",
        "hello-world",
    )


def test_parse_github_url_rejects_non_github():
    with pytest.raises(ValueError):
        parse_github_url("https://gitlab.com/octocat/hello-world")


def test_parse_github_url_rejects_incomplete_url():
    with pytest.raises(ValueError):
        parse_github_url("https://github.com/octocat")


# --- days_since ---------------------------------------------------------

def test_days_since_empty_string_returns_sentinel():
    # An empty/missing date is treated as "very old" (999) so items with
    # no timestamp sink to the bottom of the ranking instead of erroring.
    assert days_since("") == 999
    assert days_since(None) == 999


def test_days_since_recent_date():
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    assert days_since(recent) in (1, 2, 3)  # allow for test execution drift


def test_days_since_handles_z_suffix():
    # GitHub's API returns timestamps like "2024-01-01T00:00:00Z" —
    # days_since must handle the "Z" UTC suffix, not just "+00:00".
    old_date = "2020-01-01T00:00:00Z"
    assert days_since(old_date) > 365


# --- classify_issue -------------------------------------------------------

def test_classify_issue_good_first_issue_by_label():
    issue = {"title": "Fix typo", "labels": [{"name": "good first issue"}]}
    issue_type, score = classify_issue(issue)
    assert issue_type == "Good First Issue"
    assert score == 90


def test_classify_issue_bug_by_title_keyword():
    issue = {"title": "Fix crash on startup", "labels": []}
    issue_type, score = classify_issue(issue)
    assert issue_type == "Bug Fix"
    assert score == 85


def test_classify_issue_docs():
    issue = {"title": "Update documentation for API", "labels": []}
    issue_type, _ = classify_issue(issue)
    assert issue_type == "Docs"


def test_classify_issue_falls_back_to_general():
    issue = {"title": "Something unrelated", "labels": []}
    issue_type, score = classify_issue(issue)
    assert issue_type == "General"
    assert score == 50


# --- score_issue ----------------------------------------------------------

def test_score_issue_is_bounded_0_to_100():
    now = datetime.now(timezone.utc).isoformat()
    issue = {
        "title": "good first issue: fix bug",
        "labels": [{"name": "good first issue"}],
        "comments": 5,
        "updated_at": now,
        "created_at": now,
    }
    score, issue_type = score_issue(issue)
    assert 0 <= score <= 100
    assert issue_type == "Good First Issue"


def test_score_issue_stale_issue_scores_lower_than_fresh():
    fresh = {
        "title": "bug: crash",
        "labels": [],
        "comments": 0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    stale = {
        "title": "bug: crash",
        "labels": [],
        "comments": 0,
        "updated_at": "2018-01-01T00:00:00Z",
        "created_at": "2018-01-01T00:00:00Z",
    }
    fresh_score, _ = score_issue(fresh)
    stale_score, _ = score_issue(stale)
    assert fresh_score > stale_score


# --- score_repo -------------------------------------------------------

def _repo(open_issues=20, stars=500, forks=50, pushed_days_ago=1):
    pushed_at = (datetime.now(timezone.utc) - timedelta(days=pushed_days_ago)).isoformat()
    return {
        "open_issues_count": open_issues,
        "stargazers_count": stars,
        "forks_count": forks,
        "pushed_at": pushed_at,
    }


def test_score_repo_is_bounded_0_to_100():
    repo = _repo()
    score = score_repo(repo, issues=[], languages={"Python": 1000})
    assert 0 <= score <= 100


def test_score_repo_rewards_active_reasonably_sized_repo():
    active_repo = _repo(open_issues=20, stars=500, forks=50, pushed_days_ago=1)
    stale_repo = _repo(open_issues=20, stars=500, forks=50, pushed_days_ago=400)

    active_score = score_repo(active_repo, issues=[], languages={"Python": 1000})
    stale_score = score_repo(stale_repo, issues=[], languages={"Python": 1000})

    assert active_score > stale_score


def test_score_repo_no_languages_scores_lower():
    repo = _repo()
    with_lang = score_repo(repo, issues=[], languages={"Python": 1000})
    without_lang = score_repo(repo, issues=[], languages={})
    assert with_lang > without_lang


def test_score_repo_signal_report_matches_numeric_score():
    issue = {
        "title": "bug: fix API failure",
        "labels": [{"name": "bug"}],
        "comments": 2,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    repo = _repo(open_issues=20, stars=500, forks=50, pushed_days_ago=1)
    languages = {"Python": 1000}

    report = score_repo_signal_report(repo, [issue], languages)

    assert report["score"] == score_repo(repo, [issue], languages)
    assert any(item["label"] == "Recently maintained" for item in report["reasons"])
    assert any(item["label"] == "Good contributor fit" for item in report["reasons"])
    assert any(signal["label"] == "Repository Activity" for signal in report["signals_used"])


def test_issue_difficulty_estimator_varies_by_issue_type():
    assert estimate_difficulty("Docs", 95) == "Low"
    assert estimate_difficulty("Bug Fix", 90) == "Medium"
    assert estimate_difficulty("Bug Fix", 50) == "Medium/High"


# --- decision -------------------------------------------------------------

@pytest.mark.parametrize(
    "score,expected_prefix",
    [(95, "YES"), (80, "YES"), (70, "MAYBE"), (60, "MAYBE"), (30, "NO"), (0, "NO")],
)
def test_decision_thresholds(score, expected_prefix):
    assert decision(score).startswith(expected_prefix)


# --- primary_language / recommend_angle -----------------------------------

def test_primary_language_picks_highest_byte_count():
    languages = {"Python": 5000, "HTML": 200}
    assert "Python" in primary_language(languages)


def test_primary_language_empty_is_unknown():
    assert primary_language({}) == "Unknown"


def test_recommend_angle_python():
    assert "Backend" in recommend_angle({"Python": 1})


def test_recommend_angle_javascript():
    assert "Developer experience" in recommend_angle({"JavaScript": 1})


def test_recommend_angle_other_language_falls_back():
    angle = recommend_angle({"Rust": 1})
    assert "scoped bugs" in angle
