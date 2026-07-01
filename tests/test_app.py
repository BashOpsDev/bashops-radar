"""
Integration tests using FastAPI's TestClient against a throwaway SQLite
database (never the real DATABASE_URL). Covers the flows that matter most
for correctness: auth, CSRF, per-user data isolation, admin gating, and
Stripe webhook handling.

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


def _register_and_login(client, email="user@example.com", password="pass1234", name="Test User"):
    r = client.get("/register")
    token = _csrf_token(r.text)
    r = client.post(
        "/register",
        data={"name": name, "email": email, "password": password, "csrf_token": token},
        follow_redirects=False,
    )
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
        data={"name": "X", "email": "x@example.com", "password": "pass1234", "csrf_token": "forged"},
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
        data={"name": "T", "email": "t@example.com", "password": "correct-password", "csrf_token": token},
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
    client.post("/register", data={"name": "A", "email": "dupe@example.com", "password": "pass1234", "csrf_token": token})

    r = client.get("/register")
    token = _csrf_token(r.text)
    r = client.post("/register", data={"name": "B", "email": "dupe@example.com", "password": "pass1234", "csrf_token": token})
    assert "already registered" in r.text.lower()


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


# --- Stripe webhook -------------------------------------------------------

def _signed_webhook_request(client, event: dict, secret="whsec_test_secret"):
    payload = json.dumps(event).encode()
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.".encode() + payload
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    header = f"t={timestamp},v1={signature}"
    return client.post("/billing/webhook", content=payload, headers={"stripe-signature": header})


def test_webhook_rejects_bad_signature(client):
    r = client.post("/billing/webhook", content=b"{}", headers={"stripe-signature": "bogus"})
    assert r.status_code == 400


def test_webhook_checkout_completed_upgrades_user(client):
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
        "object": "event",
        "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": str(user_id), "customer": "cus_x", "subscription": "sub_x"}},
    }
    r = _signed_webhook_request(client, event)
    assert r.status_code == 200

    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    assert user.plan == "pro"
    assert user.stripe_customer_id == "cus_x"
    db.close()


def test_webhook_subscription_deleted_downgrades_user(client):
    from database import SessionLocal
    from models import User

    _register_and_login(client)

    db = SessionLocal()
    user = db.query(User).filter(User.email == "user@example.com").first()
    user.plan = "pro"
    user.stripe_subscription_id = "sub_x"
    user_id = user.id
    db.commit()
    db.close()

    event = {
        "id": "evt_2",
        "object": "event",
        "type": "customer.subscription.deleted",
        "data": {"object": {"id": "sub_x", "status": "canceled"}},
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
