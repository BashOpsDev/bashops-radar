import json
import re
from datetime import datetime, timedelta, timezone

import pytest


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _analysis(username="dev-user", github_user_id="1001", title="Fix API timeout"):
    return {
        "github_username": username,
        "github_user_id": github_user_id,
        "display_name": "Dev User",
        "avatar_url": "https://avatars.githubusercontent.com/u/1001",
        "bio": "Backend contributor",
        "public_location": "New York",
        "profile_url": f"https://github.com/{username}",
        "profile_data": {
            "public_contribution_records_analyzed": 2,
            "public_pull_requests_found": 1,
            "merged_pull_requests_found": 1,
            "open_pull_requests_found": 0,
            "public_issues_found": 1,
            "repositories_contributed_to": 1,
            "public_repositories_owned": 3,
            "is_partial": False,
            "partial_reasons": [],
            "api_disclaimer": "Based on public GitHub activity available through the GitHub API.",
        },
        "strength_data": {
            "categories": [
                {
                    "label": "Backend APIs",
                    "count": 2,
                    "percentage": 100,
                    "repositories": ["example/repo"],
                    "languages": ["Python"],
                    "examples": [],
                }
            ],
            "languages": [{"label": "Python", "count": 2}],
            "narrative": "Two public records show repeated Backend API contribution signals.",
            "portfolio_summary": (
                "Open-source contribution summary\n\n"
                "- 1 merged public pull requests found\n"
                "- Strongest contribution signals: Backend APIs\n\n"
                "Generated with BashOps Radar"
            ),
        },
        "contribution_data": [
            {
                "kind": "Pull Request",
                "title": title,
                "url": "https://github.com/example/repo/pull/7",
                "number": 7,
                "status": "Merged",
                "date": "2026-07-10T00:00:00Z",
                "repository": "example/repo",
                "repository_url": "https://github.com/example/repo",
                "language": "Python",
                "repository_stars": 500,
                "category": "Backend APIs",
                "evidence": ["Matched public contribution metadata: api."],
            },
            {
                "kind": "Public Issue",
                "title": "Document endpoint behavior",
                "url": "https://github.com/example/repo/issues/8",
                "number": 8,
                "status": "Open",
                "date": "2026-07-09T00:00:00Z",
                "repository": "example/repo",
                "repository_url": "https://github.com/example/repo",
                "language": "Python",
                "repository_stars": 500,
                "category": "Documentation",
                "evidence": ["Matched public contribution metadata: document."],
            },
        ],
    }


def _create_profile(
    username="dev-user",
    github_user_id="1001",
    user_id=None,
    claimed=False,
    public=False,
    expires_delta=timedelta(hours=24),
    analyzed_delta=timedelta(hours=1),
    analysis=None,
):
    from database import SessionLocal
    from models import DeveloperProfile

    data = analysis or _analysis(username, github_user_id)
    now = datetime.now(timezone.utc)
    profile = DeveloperProfile(
        user_id=user_id,
        github_username=username,
        github_user_id=github_user_id,
        display_name=data["display_name"],
        avatar_url=data["avatar_url"],
        bio=data["bio"],
        public_location=data["public_location"],
        profile_url=data["profile_url"],
        profile_data=data["profile_data"],
        strength_data=data["strength_data"],
        contribution_data=data["contribution_data"],
        analyzed_at=now - analyzed_delta,
        expires_at=now + expires_delta,
        is_claimed=claimed,
        is_public=public,
        public_slug=username,
    )
    db = SessionLocal()
    db.add(profile)
    db.commit()
    profile_id = profile.id
    db.close()
    return profile_id


def _login_user(
    client,
    email="dev@example.com",
    github_id="1001",
    github_username="dev-user",
    plan="free",
):
    from auth import hash_password
    from database import SessionLocal
    from models import User

    db = SessionLocal()
    user = User(
        name="Dev User",
        email=email,
        password_hash=hash_password("StrongPass1"),
        email_verified=True,
        github_id=github_id,
        github_username=github_username,
        auth_provider="email,github",
        plan=plan,
    )
    db.add(user)
    db.commit()
    user_id = user.id
    db.close()

    page = client.get("/login")
    response = client.post(
        "/login",
        data={"email": email, "password": "StrongPass1", "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return user_id


def _post_generate(client, username):
    page = client.get("/developer")
    return client.post(
        "/developer",
        data={"github_username": username, "csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )


def test_developer_landing_loads_with_evidence_copy(client):
    response = client.get("/developer")
    assert response.status_code == 200
    assert "Generate my proof-of-work profile" in response.text
    assert "Based on public GitHub" not in response.text
    assert 'name="github_username"' in response.text
    assert 'name="csrf_token"' in response.text


def test_valid_username_generates_and_persists_profile(client, monkeypatch):
    import app as app_module
    from database import SessionLocal
    from models import DeveloperProfile, Event

    monkeypatch.setattr(app_module, "analyze_developer_profile", lambda username: _analysis(username))
    response = _post_generate(client, "Dev-User")

    assert response.status_code == 303
    assert response.headers["location"] == "/developer/dev-user"
    db = SessionLocal()
    profile = db.query(DeveloperProfile).one()
    assert profile.github_username == "dev-user"
    assert profile.is_claimed is False
    assert profile.is_public is False
    event_names = {event.event_name for event in db.query(Event).all()}
    assert {"developer_profile_started", "developer_profile_generated"} <= event_names
    db.close()


def test_invalid_username_is_rejected_before_github_call(client, monkeypatch):
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "analyze_developer_profile",
        lambda username: pytest.fail("GitHub analysis should not run"),
    )
    response = _post_generate(client, "https://github.com/dev")
    assert response.status_code == 400
    assert "valid GitHub username" in response.text


@pytest.mark.parametrize(
    "code,status,message",
    [
        ("github_user_not_found", 404, "could not be found"),
        ("github_organization", 400, "not organizations"),
        ("github_rate_limit", 429, "rate limit"),
        ("github_unavailable", 503, "temporarily unavailable"),
    ],
)
def test_github_failures_render_safe_errors(client, monkeypatch, code, status, message):
    import app as app_module
    from developer_profile_service import DeveloperProfileError

    monkeypatch.setattr(
        app_module,
        "analyze_developer_profile",
        lambda username: (_ for _ in ()).throw(DeveloperProfileError(message, code)),
    )
    response = _post_generate(client, "dev-user")
    assert response.status_code == status
    assert message in response.text
    assert "Traceback" not in response.text


def test_fresh_cache_is_reused_without_github_call(client, monkeypatch):
    import app as app_module
    from database import SessionLocal
    from models import Event

    _create_profile()
    monkeypatch.setattr(
        app_module,
        "analyze_developer_profile",
        lambda username: pytest.fail("Fresh cache should be reused"),
    )
    response = _post_generate(client, "dev-user")
    assert response.status_code == 303
    assert response.headers["location"] == "/developer/dev-user"
    db = SessionLocal()
    assert db.query(Event).filter(Event.event_name == "developer_profile_cache_hit").count() == 1
    db.close()


def test_anonymous_generation_is_limited_without_blocking_cache_hits(client, monkeypatch):
    import app as app_module

    calls = []

    def analyze(username):
        calls.append(username)
        return _analysis(username=username, github_user_id=str(3000 + len(calls)))

    monkeypatch.setattr(app_module, "analyze_developer_profile", analyze)
    for username in ("developer-one", "developer-two", "developer-three"):
        assert _post_generate(client, username).status_code == 303

    blocked = _post_generate(client, "developer-four")
    assert blocked.status_code == 429
    assert "limit reached" in blocked.text
    assert calls == ["developer-one", "developer-two", "developer-three"]

    cached = _post_generate(client, "developer-one")
    assert cached.status_code == 303
    assert calls == ["developer-one", "developer-two", "developer-three"]


def test_expired_profile_refreshes_and_failure_preserves_stale_cache(client, monkeypatch):
    import app as app_module
    from database import SessionLocal
    from developer_profile_service import DeveloperProfileError
    from models import DeveloperProfile

    _create_profile(expires_delta=timedelta(hours=-1))
    refreshed = _analysis(title="Refreshed public contribution")
    monkeypatch.setattr(app_module, "analyze_developer_profile", lambda username: refreshed)
    response = _post_generate(client, "dev-user")
    assert response.status_code == 303
    db = SessionLocal()
    profile = db.query(DeveloperProfile).one()
    assert profile.contribution_data[0]["title"] == "Refreshed public contribution"
    profile.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    db.close()

    monkeypatch.setattr(
        app_module,
        "analyze_developer_profile",
        lambda username: (_ for _ in ()).throw(DeveloperProfileError("Rate limited", "github_rate_limit")),
    )
    preserved = _post_generate(client, "dev-user")
    assert preserved.status_code == 303
    page = client.get(preserved.headers["location"])
    assert "previous public snapshot is still available" in page.text
    assert "Refreshed public contribution" in page.text


def test_partial_profile_disclaimer_and_github_content_are_escaped(client):
    analysis = _analysis(title='<script>alert("x")</script>')
    analysis["display_name"] = "<img src=x onerror=alert(1)>"
    analysis["profile_data"]["is_partial"] = True
    analysis["profile_data"]["partial_reasons"] = ["GitHub marked this result <partial>."]
    _create_profile(analysis=analysis)

    response = client.get("/developer/dev-user")
    assert response.status_code == 200
    assert "Based on public GitHub activity available through the GitHub API." in response.text
    assert "&lt;script&gt;" in response.text
    assert "<script>alert" not in response.text
    assert "&lt;img src=x" in response.text
    assert "GitHub marked this result &lt;partial&gt;." in response.text


def test_service_uses_public_api_and_never_stores_email_or_bodies(monkeypatch):
    import developer_profile_service as service

    calls = []

    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append((url, params or {}))
        if url.endswith("/users/dev-user"):
            return FakeResponse(
                {
                    "id": 1001,
                    "login": "Dev-User",
                    "type": "User",
                    "name": "Dev User",
                    "email": "public@example.com",
                    "avatar_url": "https://avatars.githubusercontent.com/u/1001",
                    "html_url": "https://github.com/dev-user",
                    "public_repos": 2,
                }
            )
        if url.endswith("/users/dev-user/repos"):
            return FakeResponse([])
        if url.endswith("/search/issues") and "type:pr" in params.get("q", ""):
            return FakeResponse(
                {
                    "incomplete_results": False,
                    "items": [
                        {
                            "title": "Fix API timeout",
                            "body": "secret-looking public body that should not be stored",
                            "number": 7,
                            "state": "closed",
                            "html_url": "https://github.com/example/repo/pull/7",
                            "repository_url": "https://api.github.com/repos/example/repo",
                            "labels": [{"name": "bug"}],
                            "pull_request": {"merged_at": "2026-07-10T00:00:00Z"},
                            "updated_at": "2026-07-10T00:00:00Z",
                        }
                    ],
                }
            )
        if url.endswith("/search/issues"):
            return FakeResponse({"incomplete_results": True, "items": []})
        if url.endswith("/repos/example/repo"):
            return FakeResponse(
                {
                    "full_name": "example/repo",
                    "private": False,
                    "html_url": "https://github.com/example/repo",
                    "language": "Python",
                    "topics": ["api"],
                    "stargazers_count": 50,
                }
            )
        raise AssertionError(url)

    monkeypatch.setattr(service.requests, "get", fake_get)
    result = service.analyze_developer_profile("dev-user")
    serialized = json.dumps(result)

    assert result["profile_data"]["merged_pull_requests_found"] == 1
    assert result["strength_data"]["categories"][0]["label"] == "Backend APIs"
    assert result["profile_data"]["is_partial"] is True
    assert "public@example.com" not in serialized
    assert "secret-looking public body" not in serialized
    assert len(calls) == 5
    assert all(url.startswith("https://api.github.com/") for url, _params in calls)


def test_anonymous_and_non_owner_cannot_claim(client):
    _create_profile()
    page = client.get("/developer/dev-user")
    anonymous = client.post(
        "/developer/dev-user/claim",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert anonymous.status_code == 303
    assert anonymous.headers["location"].startswith("/login?")

    _login_user(client, email="other@example.com", github_id="2002", github_username="other")
    page = client.get("/developer/dev-user")
    denied = client.post(
        "/developer/dev-user/claim",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert denied.status_code == 403


def test_matching_github_id_claims_without_publishing(client):
    from database import SessionLocal
    from models import DeveloperProfile, Event

    user_id = _login_user(client)
    _create_profile()
    page = client.get("/developer/dev-user")
    response = client.post(
        "/developer/dev-user/claim",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db = SessionLocal()
    profile = db.query(DeveloperProfile).one()
    assert profile.user_id == user_id
    assert profile.is_claimed is True
    assert profile.is_public is False
    assert db.query(Event).filter(Event.event_name == "developer_profile_claimed").count() == 1
    db.close()


def test_owner_can_publish_unpublish_and_sitemap_tracks_only_public_profile(client):
    from database import SessionLocal
    from models import DeveloperProfile

    user_id = _login_user(client)
    _create_profile(user_id=user_id, claimed=True)
    private_page = client.get("/developer/dev-user")
    assert '<meta name="robots" content="noindex, nofollow">' in private_page.text
    assert "/developer/dev-user" not in client.get("/sitemap.xml").text

    published = client.post(
        "/developer/dev-user/publish",
        data={"csrf_token": _csrf(private_page.text)},
        follow_redirects=False,
    )
    assert published.status_code == 303
    public_page = client.get("/developer/dev-user")
    assert '<meta name="robots" content="index, follow">' in public_page.text
    assert '<link rel="canonical" href="http://testserver/developer/dev-user">' in public_page.text
    assert "/developer/dev-user" in client.get("/sitemap.xml").text

    unpublished = client.post(
        "/developer/dev-user/unpublish",
        data={"csrf_token": _csrf(public_page.text)},
        follow_redirects=False,
    )
    assert unpublished.status_code == 303
    db = SessionLocal()
    assert db.query(DeveloperProfile).one().is_public is False
    db.close()


def test_owner_refresh_is_controlled_and_delete_removes_profile(client, monkeypatch):
    import app as app_module
    from database import SessionLocal
    from models import DeveloperProfile

    user_id = _login_user(client)
    profile_id = _create_profile(user_id=user_id, claimed=True, analyzed_delta=timedelta(hours=1))
    page = client.get("/developer/dev-user")
    monkeypatch.setattr(
        app_module,
        "analyze_developer_profile",
        lambda username: pytest.fail("Recent owner refresh must not call GitHub"),
    )
    limited = client.post(
        "/developer/dev-user/refresh",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert limited.status_code == 303
    assert "refreshed recently" in client.get(limited.headers["location"]).text

    db = SessionLocal()
    profile = db.query(DeveloperProfile).filter(DeveloperProfile.id == profile_id).one()
    profile.analyzed_at = datetime.now(timezone.utc) - timedelta(hours=7)
    db.commit()
    db.close()
    monkeypatch.setattr(app_module, "analyze_developer_profile", lambda username: _analysis(title="Owner refresh"))
    page = client.get("/developer/dev-user")
    refreshed = client.post(
        "/developer/dev-user/refresh",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert refreshed.status_code == 303
    assert "Owner refresh" in client.get(refreshed.headers["location"]).text

    page = client.get("/developer/dev-user")
    deleted = client.post(
        "/developer/dev-user/delete",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    db = SessionLocal()
    assert db.query(DeveloperProfile).count() == 0
    db.close()


def test_profile_portfolio_fallback_recommendation_and_free_pro_boundaries(client):
    from database import SessionLocal
    from models import DeveloperProfile, User

    user_id = _login_user(client)
    analysis = _analysis()
    analysis["contribution_data"] = analysis["contribution_data"] * 10
    _create_profile(user_id=user_id, claimed=True, analysis=analysis)

    free_page = client.get("/developer/dev-user")
    assert "Open-source contribution summary" in free_page.text
    assert "Copy portfolio summary" in free_page.text
    assert 'href="/dashboard/opportunities"' in free_page.text
    assert "Find My Next Repository" in free_page.text
    assert "View deeper Pro history" in free_page.text
    assert free_page.text.count("Open contribution") == 12

    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).one()
    user.plan = "pro"
    db.commit()
    db.close()
    pro_page = client.get("/developer/dev-user")
    assert "View deeper Pro history" not in pro_page.text
    assert pro_page.text.count("Open contribution") == 20


def test_profile_event_endpoint_validates_csrf_and_tracks_allowlisted_actions(client):
    from database import SessionLocal
    from models import Event

    _create_profile()
    page = client.get("/developer/dev-user")
    assert client.post(
        "/developer/dev-user/event",
        data={"action": "linkedin", "csrf_token": "forged"},
    ).status_code == 403
    assert client.post(
        "/developer/dev-user/event",
        data={"action": "unknown", "csrf_token": _csrf(page.text)},
    ).status_code == 400
    tracked = client.post(
        "/developer/dev-user/event",
        data={"action": "linkedin", "csrf_token": _csrf(page.text)},
    )
    assert tracked.status_code == 200
    db = SessionLocal()
    assert db.query(Event).filter(Event.event_name == "developer_profile_shared_linkedin").count() == 1
    db.close()
