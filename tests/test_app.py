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

import pytest


def test_analysis_result_uses_issue_derived_difficulty(monkeypatch):
    import analysis_service

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
                "pushed_at": "2026-07-09T00:00:00Z",
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


def test_free_user_limit_is_two_analyses_per_day(client, monkeypatch):
    _stub_analysis(monkeypatch)
    _register_verified_and_login(client)

    assert _post_analysis(client).status_code == 200
    assert _post_analysis(client).status_code == 200

    r = _post_analysis(client)
    assert r.status_code == 200
    assert "You used your 2 free analyses today" in r.text


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


def test_api_contract_does_not_expose_score_transparency(client, monkeypatch):
    _stub_analysis(monkeypatch)

    r = client.post("/api/v1/analyze", json={"repo_url": "https://github.com/example/repo"})
    payload = r.json()

    assert r.status_code == 200
    assert "opportunity_score" in payload
    assert "score_transparency" not in payload


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
