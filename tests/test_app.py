"""
Integration tests using FastAPI's TestClient against a throwaway SQLite
database (never the real DATABASE_URL). Covers the flows that matter most
for correctness: auth, CSRF, per-user data isolation, admin gating, and
Paddle webhook handling.

Run with: pytest tests/test_app.py -v
Requires SECRET_KEY and DATABASE_URL to be set to a disposable sqlite file
— see the fixtures below, which set both automatically per test.
"""

import hmac
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone

import pytest


def test_analysis_result_uses_issue_derived_difficulty(monkeypatch):
    import analysis_service

    pushed_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")

    issue = {
        "number": 7,
        "title": "Improve setup docs",
        "html_url": "https://github.com/example/repo/issues/7",
    }

    def fake_get_analysis(repo_url):
        return (
            "example",
            "repo",
            {
                "homepage": "",
                "html_url": "https://github.com/example/repo",
                "description": "Example repo",
                "stargazers_count": 10,
                "forks_count": 2,
                "open_issues_count": 4,
                "pushed_at": pushed_at,
            },
            {"Python": 100},
            [(92, "Docs", issue)],
            80,
            "Python",
        )

    monkeypatch.setattr(analysis_service, "get_analysis", fake_get_analysis)

    result = analysis_service.build_analysis_result("https://github.com/example/repo")

    assert result["difficulty"] == "Low"
    assert result["estimated_time"].startswith("30")
    assert result["estimated_time"].endswith("60 minutes")
    assert result["merge_probability"] == "High"
    assert result["score_transparency"]["confidence"] in {"Medium", "High"}
    assert any(item["label"] == "Recently maintained" for item in result["score_transparency"]["reasons"])
    assert any(signal["label"] == "Repository Activity" for signal in result["score_transparency"]["signals_used"])

    payload = analysis_service.to_public_api_payload(result, "https://bashops.site")
    assert payload["difficulty"] == result["difficulty"]
    assert payload["estimated_time"] == result["estimated_time"]
    assert payload["merge_probability"] == result["merge_probability"]


def test_homepage_links_to_free_developer_tools(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Free Developer Tools" in r.text
    assert 'href="/tools/github-opportunity-score"' in r.text
    assert 'href="/tools/best-first-issue-finder"' in r.text
    assert "Repository Health Checker" in r.text
    assert "Should I Contribute Here?" in r.text


def test_radar_product_navigation_and_maintainer_promotion_follow_feature_flag(client, monkeypatch):
    import config

    monkeypatch.setattr(config, "MAINTAINER_ENABLED", True)
    enabled = client.get("/")
    assert enabled.status_code == 200
    assert 'href="/"' in enabled.text
    assert 'aria-current="page"' in enabled.text
    assert "BashOps Radar" in enabled.text
    assert "Current" in enabled.text
    assert 'href="/maintainer?source=radar"' in enabled.text
    assert "Explore BashOps Maintainer" in enabled.text

    monkeypatch.setattr(config, "MAINTAINER_ENABLED", False)
    disabled = client.get("/")
    assert disabled.status_code == 200
    assert 'href="/maintainer' not in disabled.text
    assert "Explore BashOps Maintainer" not in disabled.text


def test_radar_mobile_navigation_markup_and_anonymous_links_are_preserved(client, monkeypatch):
    import config

    monkeypatch.setattr(config, "MAINTAINER_ENABLED", True)
    response = client.get("/")
    assert 'id="navToggle"' in response.text
    assert 'aria-expanded="false"' in response.text
    assert 'aria-controls="navMenu"' in response.text
    assert "BashOps Radar" in response.text
    assert "BashOps Maintainer" in response.text
    assert 'href="/register"' in response.text
    assert 'href="/login"' in response.text


def test_vscode_validation_section_is_honest_and_anonymous_cta_registers(client):
    response = client.get("/")
    assert "BashOps for VS Code - Coming Soon" in response.text
    assert "The VS Code Extension is not available yet" in response.text
    assert 'href="/register">Create an Account to Join</a>' in response.text
    assert "download" not in response.text.lower()
    assert "marketplace" not in response.text.lower()


def _fake_analysis_result(score=88):
    return {
        "repo": "example/repo",
        "repo_url": "https://github.com/example/repo",
        "repo_data": {
            "description": "Example repo",
            "homepage": "",
            "html_url": "https://github.com/example/repo",
        },
        "description": "Example repo",
        "score": score,
        "score_label": "Strong",
        "score_action": "CONTRIBUTE NOW",
        "merge_probability": "High",
        "estimated_time": "3-6 hours",
        "difficulty": "Medium",
        "decision": "YES - strong Proof-of-Work target",
        "angle": "Backend reliability and API validation.",
        "best_issue": {
            "number": 7,
            "title": "Fix API failure",
            "url": "https://github.com/example/repo/issues/7",
            "score": 91,
            "type": "Bug Fix",
        },
        "recommended_action": "Start with #7 - Fix API failure",
        "recommended_outcome": "Submit one focused PR.",
        "issues": [
            (
                91,
                "Bug Fix",
                {
                    "number": 7,
                    "title": "Fix API failure",
                    "html_url": "https://github.com/example/repo/issues/7",
                },
            )
        ],
        "languages": {"Python": 100},
        "language": "Python",
        "stars": 500,
        "forks": 50,
        "open_issues": 20,
        "last_push": "2026-07-10T00:00:00Z",
        "score_transparency": {
            "reasons": [{"label": "Recently maintained", "detail": "Last push: today"}],
            "warnings": [{"label": "Review speed could not be verified", "detail": "GitHub issue data does not confirm review speed"}],
            "signals_used": [{"label": "Repository Activity", "detail": "Excellent"}],
            "confidence": "High",
            "confidence_reasons": ["Repository metadata available", "Ranked issue candidates found"],
        },
    }


def _stub_analysis(monkeypatch, score=88):
    import app as app_module

    monkeypatch.setattr(app_module, "build_analysis_result", lambda *args, **kwargs: _fake_analysis_result(score=score))
    monkeypatch.setattr(app_module, "generate_ai_summary", lambda *args, **kwargs: {"text": "Basic AI summary.", "status": "available"})


def _post_analysis(client, repo_url="https://github.com/example/repo"):
    r = client.get("/")
    token = _csrf_token(r.text)
    return client.post(
        "/analyze",
        data={"repo_url": repo_url, "csrf_token": token},
        follow_redirects=False,
    )


def _register_verified_and_login(client, email="user@example.com", password="StrongPass1", name="Test User"):
    from database import SessionLocal
    from models import User

    r = client.get("/register")
    token = _csrf_token(r.text)
    r = client.post(
        "/register",
        data={"name": name, "email": email, "password": password, "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 200

    db = SessionLocal()
    user = db.query(User).filter(User.email == email).first()
    user.email_verified = True
    db.commit()
    db.close()

    r = client.get("/login")
    token = _csrf_token(r.text)
    r = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_authenticated_navigation_and_vscode_interest_are_preserved_and_deduplicated(client):
    from database import SessionLocal
    from models import Event, User

    _register_verified_and_login(client)
    page = client.get("/")
    assert 'href="/dashboard"' in page.text
    assert 'href="/logout"' in page.text
    assert "Notify Me" in page.text

    token = _csrf_token(page.text)
    first = client.post(
        "/vscode-interest",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert first.status_code == 303
    assert first.headers["location"] == "/?vscode_interest=joined#vscode-extension"

    page = client.get("/")
    token = _csrf_token(page.text)
    repeated = client.post(
        "/vscode-interest",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert repeated.status_code == 303
    assert repeated.headers["location"] == "/?vscode_interest=already#vscode-extension"

    db = SessionLocal()
    user = db.query(User).filter(User.email == "user@example.com").first()
    interests = db.query(Event).filter(
        Event.user_id == user.id,
        Event.event_name == "vscode_interest_submitted",
    ).all()
    clicks = db.query(Event).filter(
        Event.user_id == user.id,
        Event.event_name == "vscode_waitlist_clicked",
    ).count()
    assert len(interests) == 1
    assert clicks == 2
    assert "user@example.com" not in (interests[0].metadata_json or "")
    db.close()

    joined = client.get(first.headers["location"].split("#", 1)[0])
    assert 'role="status"' in joined.text
    assert "registered for BashOps for VS Code updates" in joined.text


def test_vscode_interest_requires_csrf(client):
    from database import SessionLocal
    from models import Event

    _register_verified_and_login(client)
    response = client.post(
        "/vscode-interest",
        data={"csrf_token": "forged"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/?vscode_interest=error#vscode-extension"

    db = SessionLocal()
    assert db.query(Event).filter(Event.event_name == "vscode_interest_submitted").count() == 0
    db.close()

    error_page = client.get(response.headers["location"].split("#", 1)[0])
    assert 'role="alert"' in error_page.text


def test_cross_product_links_record_only_safe_events(client, monkeypatch):
    import config
    from database import SessionLocal
    from models import Event

    monkeypatch.setattr(config, "MAINTAINER_ENABLED", True)
    assert client.get("/maintainer?source=radar").status_code == 200
    assert client.get("/?source=maintainer").status_code == 200

    db = SessionLocal()
    events = db.query(Event).filter(
        Event.event_name.in_(["radar_to_maintainer_clicked", "maintainer_to_radar_clicked"])
    ).all()
    assert {event.event_name for event in events} == {
        "radar_to_maintainer_clicked",
        "maintainer_to_radar_clicked",
    }
    assert all("@" not in (event.metadata_json or "") for event in events)
    db.close()


def test_anonymous_visitor_gets_one_full_analysis_then_registration_cta(client, monkeypatch):
    _stub_analysis(monkeypatch)

    r = _post_analysis(client)
    assert r.status_code == 200
    assert "Opportunity Report" in r.text

    r = _post_analysis(client)
    assert r.status_code == 200
    assert "Your first free analysis is complete." in r.text
    assert "Create Free Account" in r.text
    assert "Log In" in r.text


def test_free_user_limit_is_two_lifetime_analyses(client, monkeypatch):
    _stub_analysis(monkeypatch)
    _register_verified_and_login(client)

    assert _post_analysis(client).status_code == 200
    assert _post_analysis(client).status_code == 200

    r = _post_analysis(client)
    assert r.status_code == 200
    assert "Your free analysis trial is complete." in r.text
    assert "You have used both full-quality repository analyses" in r.text
    assert "Upgrade to Pro" in r.text
    assert "View My Pipeline" in r.text


def test_failed_analysis_does_not_consume_free_lifetime_quota(client, monkeypatch):
    import app as app_module

    calls = {"count": 0}

    def fake_build_analysis_result(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ValueError("Please provide a valid GitHub repository URL.")
        return _fake_analysis_result()

    monkeypatch.setattr(app_module, "build_analysis_result", fake_build_analysis_result)
    monkeypatch.setattr(app_module, "generate_ai_summary", lambda *args, **kwargs: {"text": "Basic AI summary.", "status": "available"})
    _register_verified_and_login(client)

    r = _post_analysis(client, repo_url="not-a-repo")
    assert r.status_code == 200
    assert "We could not analyze that repository." in r.text

    assert _post_analysis(client).status_code == 200
    assert _post_analysis(client).status_code == 200

    r = _post_analysis(client)
    assert "Your free analysis trial is complete." in r.text


def test_existing_free_user_with_two_prior_targets_is_blocked(client, monkeypatch):
    from database import SessionLocal
    from models import Target, User

    _stub_analysis(monkeypatch)
    _register_verified_and_login(client)

    db = SessionLocal()
    user = db.query(User).filter(User.email == "user@example.com").first()
    db.add(Target(user_id=user.id, repo="example/one", repo_url="", language="Python", score=80))
    db.add(Target(user_id=user.id, repo="example/two", repo_url="", language="Python", score=81))
    db.commit()
    db.close()

    r = _post_analysis(client)
    assert r.status_code == 200
    assert "Your free analysis trial is complete." in r.text


def test_dashboard_and_pricing_use_lifetime_trial_wording(client, monkeypatch):
    _stub_analysis(monkeypatch)
    _register_verified_and_login(client)

    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "2 free analyses remaining." in r.text
    assert "analyses remaining today" not in r.text
    assert "2 analyses/day" not in r.text

    _post_analysis(client)
    r = client.get("/dashboard")
    assert "1 free analysis remaining." in r.text

    _post_analysis(client)
    r = client.get("/dashboard")
    assert "Free analysis trial complete." in r.text

    r = client.get("/pricing")
    assert "2 free analyses included" in r.text
    assert "2 analyses/day" not in r.text


def test_unverified_user_must_verify_before_account_analysis(client, monkeypatch):
    from database import SessionLocal
    from models import Target, User

    _stub_analysis(monkeypatch)

    r = client.get("/register")
    token = _csrf_token(r.text)
    r = client.post(
        "/register",
        data={"name": "Trial User", "email": "trial@example.com", "password": "StrongPass1", "csrf_token": token},
    )
    assert r.status_code == 200

    r = client.get("/login")
    token = _csrf_token(r.text)
    r = client.post(
        "/login",
        data={"email": "trial@example.com", "password": "StrongPass1", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Please verify your email before logging in." in r.text

    db = SessionLocal()
    user = db.query(User).filter(User.email == "trial@example.com").first()
    verify_token = user.email_verification_token
    assert db.query(Target).filter(Target.user_id == user.id).count() == 0
    db.close()

    r = client.get(f"/verify-email?token={verify_token}")
    assert r.status_code == 200

    r = client.get("/login")
    token = _csrf_token(r.text)
    r = client.post(
        "/login",
        data={"email": "trial@example.com", "password": "StrongPass1", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303

    r = _post_analysis(client)
    assert r.status_code == 200
    assert "Opportunity Report" in r.text


def test_pro_user_remains_unlimited(client, monkeypatch):
    from database import SessionLocal
    from models import User

    _stub_analysis(monkeypatch)
    _register_verified_and_login(client)

    db = SessionLocal()
    user = db.query(User).filter(User.email == "user@example.com").first()
    user.plan = "pro"
    db.commit()
    db.close()

    for _ in range(3):
        r = _post_analysis(client)
        assert r.status_code == 200
        assert "Free Limit Reached" not in r.text


def test_free_analysis_shows_core_result_and_safe_pro_preview(client, monkeypatch):
    _stub_analysis(monkeypatch, score=88)
    _register_verified_and_login(client)

    r = _post_analysis(client)

    assert "Why this repository scored well" in r.text
    assert "Signals Used" in r.text
    assert "Confidence" in r.text
    assert "Best First Issue" in r.text
    assert "Basic AI summary." in r.text
    assert "Estimated Merge Probability" in r.text
    assert "Estimated Implementation Time" in r.text
    assert "Estimated Difficulty" in r.text
    assert "Estimated Contract Potential" in r.text
    assert ">Merge Probability<" not in r.text
    assert ">Estimated Time<" not in r.text
    assert "This repository scored 88." in r.text
    assert "Founder Outreach Strategy" in r.text
    assert "Unlock Pro" in r.text
    assert "Pro founder outreach workflows are active" not in r.text


def test_low_score_analysis_uses_discovery_upgrade_copy(client, monkeypatch):
    _stub_analysis(monkeypatch, score=32)
    _register_verified_and_login(client)

    r = _post_analysis(client)

    assert "Why this repository scored low" in r.text
    assert "This repository may not be worth your time." in r.text
    assert "discover stronger opportunities" in r.text
    assert "Discover Better Repositories" in r.text
    assert "similar signals" not in r.text


def test_pro_analysis_shows_pro_state_not_locked_preview(client, monkeypatch):
    from database import SessionLocal
    from models import User

    _stub_analysis(monkeypatch)
    _register_verified_and_login(client)

    db = SessionLocal()
    user = db.query(User).filter(User.email == "user@example.com").first()
    user.plan = "pro"
    db.commit()
    db.close()

    r = _post_analysis(client)

    assert "Pro founder outreach workflows are active" in r.text
    assert "Founder Outreach Strategy" not in r.text


def test_pipeline_saved_target_reopens_full_analysis_without_duplicate(client, monkeypatch):
    from database import SessionLocal
    from models import Target, User

    _stub_analysis(monkeypatch, score=86)
    _register_verified_and_login(client)

    db = SessionLocal()
    user = db.query(User).filter(User.email == "user@example.com").first()
    target = Target(
        user_id=user.id,
        repo="example/repo",
        repo_url="https://github.com/example/repo",
        language="Python",
        score=86,
        best_issue="#7",
        best_issue_url="https://github.com/example/repo/issues/7",
    )
    db.add(target)
    db.commit()
    target_id = target.id
    before_count = db.query(Target).filter(Target.user_id == user.id).count()
    db.close()

    r = client.get("/pipeline")
    assert f'href="/analysis/{target_id}"' in r.text
    assert "View Full Analysis" in r.text

    r = client.get(f"/analysis/{target_id}")
    assert r.status_code == 200
    assert "Why this repository scored well" in r.text
    assert "Best First Issue" in r.text
    assert "Basic AI summary." in r.text

    db = SessionLocal()
    after_count = db.query(Target).filter(Target.user_id == user.id).count()
    db.close()
    assert after_count == before_count


def _create_target_for_user(email, repo="example/repo", status="Researching", score=86, pitch=""):
    from database import SessionLocal
    from models import Target, User

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        target = Target(
            user_id=user.id,
            repo=repo,
            repo_url=f"https://github.com/{repo}",
            language="Python",
            score=score,
            status=status,
            best_issue="#7",
            best_issue_url=f"https://github.com/{repo}/issues/7",
            merge_probability="High",
            difficulty="Medium",
            estimated_time="3-6 hours",
            pitch=pitch,
            stars=500,
            forks=50,
            open_issues=20,
        )
        db.add(target)
        db.commit()
        target_id = target.id
    finally:
        db.close()

    return target_id


def test_snapshot_route_requires_login(client):
    r = client.get("/pipeline/1/snapshot", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_free_user_can_view_first_researching_snapshot(client, monkeypatch):
    _stub_analysis(monkeypatch, score=86)
    _register_verified_and_login(client)
    target_id = _create_target_for_user("user@example.com", pitch="private founder pitch")

    r = client.get("/pipeline")
    assert f'href="/pipeline/{target_id}/snapshot"' in r.text
    assert "View Snapshot" in r.text
    assert "Update Status" in r.text

    r = client.get(f"/pipeline/{target_id}/snapshot")
    assert r.status_code == 200
    assert 'name="robots" content="noindex, nofollow"' in r.text
    assert "BashOps Radar" in r.text
    assert "Proof-of-Work Snapshot" in r.text
    assert "example/repo" in r.text
    assert "86" in r.text
    assert "Repository Signals" in r.text
    assert "private founder pitch" not in r.text
    assert "#7" not in r.text


def test_snapshot_route_blocks_other_users_target(client, monkeypatch):
    _stub_analysis(monkeypatch)
    _register_verified_and_login(client, email="owner@example.com")
    target_id = _create_target_for_user("owner@example.com")
    client.get("/logout")

    _register_verified_and_login(client, email="other@example.com")
    r = client.get(f"/pipeline/{target_id}/snapshot", follow_redirects=False)
    assert r.status_code == 404


def test_free_user_second_snapshot_is_locked(client, monkeypatch):
    _stub_analysis(monkeypatch)
    _register_verified_and_login(client)
    first_id = _create_target_for_user("user@example.com", repo="example/first")
    second_id = _create_target_for_user("user@example.com", repo="example/second")

    r = client.get(f"/pipeline/{first_id}/snapshot")
    assert r.status_code == 200
    assert "Proof-of-Work Snapshot is available for your first researched repository" not in r.text

    r = client.get(f"/pipeline/{second_id}/snapshot")
    assert r.status_code == 200
    assert "Proof-of-Work Snapshot is available for your first researched repository" in r.text
    assert "Upgrade to Pro" in r.text
    assert "example/second" not in r.text


def test_free_user_later_stage_snapshot_is_locked(client, monkeypatch):
    _stub_analysis(monkeypatch)
    _register_verified_and_login(client)
    target_id = _create_target_for_user("user@example.com", status="PR Submitted")

    r = client.get(f"/pipeline/{target_id}/snapshot")
    assert r.status_code == 200
    assert "Proof-of-Work Snapshot is available for your first researched repository" in r.text
    assert "PR Submitted snapshots" in r.text
    assert "example/repo" not in r.text


def test_pro_user_can_view_multiple_and_later_stage_snapshots(client, monkeypatch):
    from database import SessionLocal
    from models import User

    _stub_analysis(monkeypatch)
    _register_verified_and_login(client)

    db = SessionLocal()
    user = db.query(User).filter(User.email == "user@example.com").first()
    user.plan = "pro"
    db.commit()
    db.close()

    first_id = _create_target_for_user("user@example.com", repo="example/first", status="Researching")
    second_id = _create_target_for_user("user@example.com", repo="example/second", status="PR Merged", score=91)

    r = client.get(f"/pipeline/{first_id}/snapshot")
    assert r.status_code == 200
    assert "example/first" in r.text
    assert "Researching" in r.text

    r = client.get(f"/pipeline/{second_id}/snapshot")
    assert r.status_code == 200
    assert "example/second" in r.text
    assert "PR Merged" in r.text
    assert "The contribution has been accepted and merged." in r.text
    assert "Proof-of-Work Snapshot is available for your first researched repository" not in r.text

def test_api_contract_does_not_expose_score_transparency(client, monkeypatch):
    _stub_analysis(monkeypatch)

    r = client.post("/api/v1/analyze", json={"repo_url": "https://github.com/example/repo"})
    payload = r.json()

    assert r.status_code == 200
    assert "opportunity_score" in payload
    assert "score_transparency" not in payload


def test_public_api_keeps_existing_anonymous_quota_behavior(client, monkeypatch):
    _stub_analysis(monkeypatch)

    assert client.post("/api/v1/analyze", json={"repo_url": "https://github.com/example/repo"}).status_code == 200
    assert client.post("/api/v1/analyze", json={"repo_url": "https://github.com/example/repo"}).status_code == 200

    r = client.post("/api/v1/analyze", json={"repo_url": "https://github.com/example/repo"})
    assert r.status_code == 429
    assert "upgrade_url" in r.json()


def _csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf_token not found in response HTML"
    return match.group(1)


def _register_and_login(client, email="user@example.com", password="StrongPass1", name="Test User"):
    r = client.get("/register")
    token = _csrf_token(r.text)
    r = client.post(
        "/register",
        data={"name": name, "email": email, "password": password, "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 200

    from database import SessionLocal
    from models import User

    db = SessionLocal()
    user = db.query(User).filter(User.email == email).first()
    verify_token = user.email_verification_token
    db.close()

    r = client.get(f"/verify-email?token={verify_token}", follow_redirects=False)
    assert r.status_code == 200
    assert "Email verified" in r.text

    r = client.get("/login")
    token = _csrf_token(r.text)
    r = client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return client


# --- Auth / CSRF ------------------------------------------------------

def test_register_requires_valid_csrf(client):
    r = client.post(
        "/register",
        data={"name": "X", "email": "x@example.com", "password": "StrongPass1", "csrf_token": "forged"},
        follow_redirects=False,
    )
    assert r.status_code == 200  # re-renders the form with an error, no redirect
    assert "expired" in r.text.lower() or "error" in r.text.lower()


def test_register_then_login_flow(client):
    _register_and_login(client)
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Welcome back," in r.text
    assert "Good morning" not in r.text


def test_login_wrong_password_rejected(client):
    r = client.get("/register")
    token = _csrf_token(r.text)
    client.post(
        "/register",
        data={"name": "T", "email": "t@example.com", "password": "CorrectPass1", "csrf_token": token},
    )

    r = client.get("/login")
    token = _csrf_token(r.text)
    r = client.post(
        "/login",
        data={"email": "t@example.com", "password": "wrong-password", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Invalid" in r.text


def test_duplicate_email_registration_rejected(client):
    r = client.get("/register")
    token = _csrf_token(r.text)
    client.post("/register", data={"name": "A", "email": "dupe@example.com", "password": "StrongPass1", "csrf_token": token})

    r = client.get("/register")
    token = _csrf_token(r.text)
    r = client.post("/register", data={"name": "B", "email": "dupe@example.com", "password": "StrongPass1", "csrf_token": token})
    assert "already registered" in r.text.lower()


def test_weak_password_rejected(client):
    r = client.get("/register")
    token = _csrf_token(r.text)
    r = client.post(
        "/register",
        data={"name": "Weak", "email": "weak@example.com", "password": "password", "csrf_token": token},
    )
    assert r.status_code == 200
    assert "uppercase" in r.text.lower() or "number" in r.text.lower()


def test_unverified_user_cannot_login(client):
    r = client.get("/register")
    token = _csrf_token(r.text)
    client.post(
        "/register",
        data={"name": "Unverified", "email": "unverified@example.com", "password": "StrongPass1", "csrf_token": token},
    )

    r = client.get("/login")
    token = _csrf_token(r.text)
    r = client.post(
        "/login",
        data={"email": "unverified@example.com", "password": "StrongPass1", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "verify your email" in r.text.lower()


def test_verification_token_verifies_user(client):
    from database import SessionLocal
    from models import User

    r = client.get("/register")
    token = _csrf_token(r.text)
    client.post(
        "/register",
        data={"name": "Verify", "email": "verify@example.com", "password": "StrongPass1", "csrf_token": token},
    )

    db = SessionLocal()
    user = db.query(User).filter(User.email == "verify@example.com").first()
    verify_token = user.email_verification_token
    assert user.email_verified is False
    db.close()

    r = client.get(f"/verify-email?token={verify_token}", follow_redirects=False)
    assert r.status_code == 200
    assert "Email verified" in r.text

    db = SessionLocal()
    user = db.query(User).filter(User.email == "verify@example.com").first()
    assert user.email_verified is True
    assert user.email_verification_token is None
    db.close()


def test_forgot_password_generic_response(client):
    r = client.get("/forgot-password")
    token = _csrf_token(r.text)
    r = client.post(
        "/forgot-password",
        data={"email": "nobody@example.com", "csrf_token": token},
    )
    assert r.status_code == 200
    assert "if an account exists" in r.text.lower()


def test_reset_password_works_with_valid_token(client):
    from database import SessionLocal
    from models import User

    _register_and_login(client, email="reset@example.com", password="OldPass1")
    client.get("/logout")

    r = client.get("/forgot-password")
    token = _csrf_token(r.text)
    client.post(
        "/forgot-password",
        data={"email": "reset@example.com", "csrf_token": token},
    )

    db = SessionLocal()
    user = db.query(User).filter(User.email == "reset@example.com").first()
    reset_token = user.password_reset_token
    db.close()

    r = client.get(f"/reset-password?token={reset_token}")
    token = _csrf_token(r.text)
    r = client.post(
        "/reset-password",
        data={"token": reset_token, "password": "NewPass1", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login?reset=true"

    r = client.get("/login")
    token = _csrf_token(r.text)
    r = client.post(
        "/login",
        data={"email": "reset@example.com", "password": "NewPass1", "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303


# --- Protected routes ---------------------------------------------------

@pytest.mark.parametrize("path", ["/dashboard", "/pipeline", "/export-pipeline"])
def test_protected_routes_redirect_when_logged_out(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


# --- Per-user data isolation ---------------------------------------------

def test_users_do_not_see_each_others_pipeline_data(client):
    from database import SessionLocal
    from models import Target, User

    _register_and_login(client, email="alice@example.com", name="Alice")

    db = SessionLocal()
    alice = db.query(User).filter(User.email == "alice@example.com").first()
    db.add(Target(user_id=alice.id, repo="alice/only-mine", repo_url="", language="Python", score=90))
    db.commit()
    db.close()

    client.get("/logout")

    _register_and_login(client, email="bob@example.com", name="Bob")
    r = client.get("/pipeline")
    assert "alice/only-mine" not in r.text


def test_admin_analytics_blocked_for_non_admin(client):
    _register_and_login(client)
    r = client.get("/admin/analytics", follow_redirects=False)
    assert r.status_code == 403


def test_admin_analytics_allowed_for_admin(client, monkeypatch):
    import config

    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    monkeypatch.setattr(config, "ADMIN_EMAILS", ["admin@example.com"])
    _register_and_login(client, email="admin@example.com", name="Admin")
    r = client.get("/admin/analytics")
    assert r.status_code == 200


def test_admin_users_blocked_for_non_admin(client):
    _register_and_login(client)
    r = client.get("/admin/users", follow_redirects=False)
    assert r.status_code == 403


def test_admin_users_allowed_for_admin(client, monkeypatch):
    import config

    monkeypatch.setenv("ADMIN_EMAILS", "admin@example.com")
    monkeypatch.setattr(config, "ADMIN_EMAILS", ["admin@example.com"])
    _register_and_login(client, email="admin@example.com", name="Admin")
    r = client.get("/admin/users")
    assert r.status_code == 200
    assert "Total Users" in r.text


def test_github_callback_rejects_invalid_state(client, monkeypatch):
    import config

    monkeypatch.setattr(config, "GITHUB_CLIENT_ID", "client")
    monkeypatch.setattr(config, "GITHUB_CLIENT_SECRET", "secret")
    monkeypatch.setattr(config, "GITHUB_OAUTH_REDIRECT_URI", "https://example.com/auth/github/callback")
    monkeypatch.setattr(config, "github_oauth_configured", True)

    r = client.get("/auth/github/callback?code=abc&state=bad")
    assert r.status_code == 200
    assert "session expired" in r.text.lower()


def test_analyze_requires_valid_csrf(client):
    r = client.post(
        "/analyze",
        data={"repo_url": "https://github.com/octocat/hello-world", "csrf_token": "forged"},
    )
    assert r.status_code == 200
    assert "session expired" in r.text.lower()


def test_navbar_hides_admin_link_for_regular_users(client):
    _register_and_login(client)
    r = client.get("/pipeline")
    assert 'href="/admin/analytics"' not in r.text


# --- Pro-gated pitch generation -------------------------------------------

def test_free_user_cannot_generate_founder_pitch(client):
    from database import SessionLocal
    from models import Target, User

    _register_and_login(client)

    db = SessionLocal()
    user = db.query(User).filter(User.email == "user@example.com").first()
    db.add(Target(user_id=user.id, repo="octocat/hello", repo_url="", language="Python", score=80, pitch=""))
    db.commit()
    db.close()

    r = client.get("/pipeline")
    token = _csrf_token(r.text)
    r = client.post(
        "/generate-pitch",
        data={"repo": "octocat/hello", "best_issue": "#1", "csrf_token": token},
    )
    assert r.status_code == 200
    assert "Founder pitch generation is locked" in r.text
    assert "Founder pitch generation is a Pro feature" in r.text
    assert "I reviewed octocat/hello" not in r.text


def test_pro_user_can_generate_and_persist_founder_pitch(client, monkeypatch):
    from database import SessionLocal
    from models import Target, User

    import app

    monkeypatch.setattr(app, "generate_pitch", lambda repo, best_issue: f"Pro pitch for {repo} {best_issue}")
    _register_and_login(client)

    db = SessionLocal()
    user = db.query(User).filter(User.email == "user@example.com").first()
    user.plan = "pro"
    db.add(Target(user_id=user.id, repo="octocat/hello", repo_url="", language="Python", score=80, pitch=""))
    db.commit()
    db.close()

    r = client.get("/pipeline")
    token = _csrf_token(r.text)
    r = client.post(
        "/generate-pitch",
        data={"repo": "octocat/hello", "best_issue": "#1", "csrf_token": token},
    )
    assert r.status_code == 200
    assert "Pro pitch for octocat/hello #1" in r.text

    db = SessionLocal()
    row = db.query(Target).filter(Target.repo == "octocat/hello").first()
    assert row.pitch == "Pro pitch for octocat/hello #1"
    db.close()


# --- Paddle webhook -------------------------------------------------------

def _signed_webhook_request(client, event: dict, secret="paddle_test_secret"):
    payload = json.dumps(event).encode()
    timestamp = int(time.time())
    signed_payload = f"{timestamp}:".encode() + payload
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    header = f"ts={timestamp};h1={signature}"
    return client.post("/billing/webhook", content=payload, headers={"Paddle-Signature": header})


def test_webhook_rejects_bad_signature(client):
    r = client.post("/billing/webhook", content=b"{}", headers={"Paddle-Signature": "bogus"})
    assert r.status_code == 400


def test_webhook_transaction_completed_upgrades_user(client):
    from database import SessionLocal
    from models import User

    _register_and_login(client)

    db = SessionLocal()
    user = db.query(User).filter(User.email == "user@example.com").first()
    user_id = user.id
    assert user.plan == "free"
    db.close()

    event = {
        "id": "evt_1",
        "event_type": "transaction.completed",
        "data": {
            "custom_data": {"user_id": str(user_id)},
            "customer_id": "ctm_x",
            "subscription_id": "sub_x",
            "status": "completed",
        },
    }
    r = _signed_webhook_request(client, event)
    assert r.status_code == 200

    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    assert user.plan == "pro"
    assert user.paddle_customer_id == "ctm_x"
    db.close()


def test_webhook_subscription_canceled_downgrades_user(client):
    from database import SessionLocal
    from models import User

    _register_and_login(client)

    db = SessionLocal()
    user = db.query(User).filter(User.email == "user@example.com").first()
    user.plan = "pro"
    user.paddle_subscription_id = "sub_x"
    user_id = user.id
    db.commit()
    db.close()

    event = {
        "id": "evt_2",
        "event_type": "subscription.canceled",
        "data": {"id": "sub_x", "status": "canceled"},
    }
    r = _signed_webhook_request(client, event)
    assert r.status_code == 200

    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    assert user.plan == "free"
    db.close()


# --- Error pages -----------------------------------------------------

def test_404_page_for_unknown_route(client):
    r = client.get("/this-route-does-not-exist")
    assert r.status_code == 404
    assert "doesn't exist" in r.text.lower() or "404" in r.text


# --- SEO -----------------------------------------------------------------

def test_robots_txt_disallows_private_routes(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Disallow: /dashboard" in r.text


def test_sitemap_contains_public_pages(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "<loc>" in r.text
    assert "/tools/github-opportunity-score" in r.text
    assert "/tools/best-first-issue-finder" in r.text


def test_github_opportunity_score_tool_loads(client):
    r = client.get("/tools/github-opportunity-score")
    assert r.status_code == 200
    assert "GitHub Opportunity Score" in r.text
    assert 'action="/analyze"' in r.text
    assert 'name="csrf_token"' in r.text


def test_best_first_issue_finder_tool_loads(client):
    r = client.get("/tools/best-first-issue-finder")
    assert r.status_code == 200
    assert "Best First Issue Finder" in r.text
    assert 'action="/analyze"' in r.text
    assert 'name="csrf_token"' in r.text
