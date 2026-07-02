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
    assert r.status_code == 303

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
    assert r.status_code == 303

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
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


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
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


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

def test_free_user_gets_static_pitch_not_persisted_as_ai(client):
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
    assert "proof-of-work opportunity" in r.text  # the free fallback template


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
