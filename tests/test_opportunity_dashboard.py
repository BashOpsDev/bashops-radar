import re
from datetime import datetime, timedelta, timezone

import pytest


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match
    return match.group(1)


@pytest.fixture(autouse=True)
def _avoid_live_refresh(monkeypatch):
    import app

    monkeypatch.setattr(app, "_opportunity_refresh_status", lambda: {"status": "cache_hit", "updated": 0, "failed": 0})


def _login(client, email="developer@example.com", plan="free"):
    from auth import hash_password
    from database import SessionLocal
    from models import User

    db = SessionLocal()
    user = User(
        name="Developer",
        email=email,
        password_hash=hash_password("StrongPass1"),
        email_verified=True,
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


def _feed_item(
    full_name="example/repo",
    score=88,
    language="Python",
    categories=None,
    expires_delta=timedelta(hours=12),
    description="A safe repository description",
    best_issue=True,
):
    from database import SessionLocal
    from models import OpportunityFeedItem

    now = datetime.now(timezone.utc)
    owner, name = full_name.split("/", 1)
    item = OpportunityFeedItem(
        repository_full_name=full_name,
        repository_url=f"https://github.com/{full_name}",
        repository_owner=owner,
        repository_name=name,
        description=description,
        primary_language=language,
        categories=categories or ["Backend APIs"],
        topics=["api", "testing"],
        radar_score=score,
        decision="YES - strong Proof-of-Work target",
        best_issue_number=7 if best_issue else None,
        best_issue_title="Fix API timeout" if best_issue else None,
        best_issue_url=f"https://github.com/{full_name}/issues/7" if best_issue else None,
        difficulty="Medium",
        merge_probability="High",
        maintainer_activity_signal="Maintenance activity signal: High",
        recent_activity_signal="Repository pushed 2 days ago",
        commercial_signal="Organization-owned metadata. This does not prove commercial intent.",
        paid_sprint_signal="Potential paid-sprint signal: commercial metadata and a strong Radar score are present",
        public_reason="Recent repository activity and a ranked issue are available.",
        source_snapshot={"changes": ["New to today's bounded opportunity pool"]},
        analyzed_at=now - timedelta(hours=1),
        expires_at=now + expires_delta,
        first_seen_at=now - timedelta(hours=1),
        last_seen_at=now - timedelta(hours=1),
        is_active=True,
    )
    db = SessionLocal()
    db.add(item)
    db.commit()
    db.refresh(item)
    item_id = item.id
    db.close()
    return item_id


def _profile(user_id, language="Python", category="Backend APIs"):
    from database import SessionLocal
    from models import DeveloperProfile

    now = datetime.now(timezone.utc)
    profile = DeveloperProfile(
        user_id=user_id,
        github_username=f"developer-{user_id}",
        github_user_id=str(1000 + user_id),
        display_name="Developer",
        avatar_url="",
        bio="",
        public_location="",
        profile_url=f"https://github.com/developer-{user_id}",
        profile_data={},
        strength_data={
            "languages": [{"label": language, "count": 3}],
            "categories": [{"label": category, "count": 3}],
            "portfolio_summary": "Public contribution evidence",
        },
        contribution_data=[],
        analyzed_at=now,
        expires_at=now + timedelta(hours=24),
        is_claimed=True,
        is_public=False,
        public_slug=f"developer-{user_id}",
    )
    db = SessionLocal()
    db.add(profile)
    db.commit()
    db.close()


def _analysis_result(full_name="example/repo", score=91):
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "repo": full_name,
        "repo_url": f"https://github.com/{full_name}",
        "repo_data": {
            "html_url": f"https://github.com/{full_name}",
            "description": "API project",
            "topics": ["api", "testing"],
        },
        "description": "API project",
        "language": "Python",
        "score": score,
        "decision": "YES - strong Proof-of-Work target",
        "best_issue": {
            "number": 7,
            "title": "Fix API timeout",
            "url": f"https://github.com/{full_name}/issues/7",
            "type": "Backend APIs",
        },
        "difficulty": "Medium",
        "merge_probability": "High",
        "open_issues": 20,
        "last_push": now,
        "issues": [(90, "Backend APIs", {"number": 7, "updated_at": now})],
        "score_transparency": {
            "reasons": [{"label": "Active maintenance", "detail": "Last push: today"}],
            "signals_used": [{"label": "Maintenance Activity", "detail": "High"}],
        },
        "repository_intelligence": [
            {"key": "commercial", "value": "Signals present", "detail": "Organization-owned metadata."},
            {"key": "friendliness", "value": "Strong", "detail": "Contributor-friendly labels."},
            {"key": "momentum", "value": "Growing", "detail": "Recent activity."},
        ],
    }


def test_public_today_loads_safe_indexable_opportunities_and_sitemap(client):
    _feed_item(description='<script>alert("x")</script>')
    response = client.get("/today")

    assert response.status_code == 200
    assert "Today's GitHub Opportunities" in response.text
    assert "&lt;script&gt;" in response.text
    assert '<link rel="canonical" href="http://testserver/today">' in response.text
    assert "not confirmed jobs" in response.text
    assert "/today" in client.get("/sitemap.xml").text


def test_public_today_empty_and_stale_states_are_safe(client, monkeypatch):
    empty = client.get("/today")
    assert "daily candidate pool is being refreshed" in empty.text

    _feed_item(expires_delta=timedelta(hours=-1))
    monkeypatch.setattr("app._opportunity_refresh_status", lambda: {"status": "stale", "updated": 0, "failed": 1})
    stale = client.get("/today")
    assert "latest cached report is shown" in stale.text
    assert "example/repo" in stale.text


def test_dashboard_requires_authentication_and_missing_profile_shows_cta(client):
    anonymous = client.get("/dashboard/opportunities", follow_redirects=False)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"].startswith("/login?")

    _login(client)
    _feed_item()
    response = client.get("/dashboard/opportunities")
    assert response.status_code == 200
    assert "Generate your Developer Profile" in response.text
    assert 'meta name="robots" content="noindex,nofollow"' in response.text


def test_profile_match_is_distinct_deterministic_and_changes_order(client):
    user_id = _login(client)
    _profile(user_id)
    _feed_item("example/typescript", score=95, language="TypeScript", categories=["Frontend"])
    _feed_item("example/python", score=82, language="Python", categories=["Backend APIs"])

    response = client.get("/dashboard/opportunities")
    assert response.status_code == 200
    assert "82/100 Radar" in response.text
    assert re.search(r"<strong>\s*60\s*</strong><small>/100 match signal</small>", response.text)
    assert "Matches your Python contribution history" in response.text
    assert response.text.index("example/python") < response.text.index("example/typescript")


def test_free_and_pro_recommendation_limits(client):
    _login(client)
    for index in range(7):
        _feed_item(f"example/free-{index}", score=90 - index)
    free_page = client.get("/dashboard/opportunities")
    assert free_page.text.count("daily-opportunity-card") == 5

    from database import SessionLocal
    from models import User

    db = SessionLocal()
    user = db.query(User).filter(User.email == "developer@example.com").first()
    user.plan = "pro"
    db.commit()
    db.close()
    pro_page = client.get("/dashboard/opportunities")
    assert pro_page.text.count("daily-opportunity-card") == 7


def test_save_dismiss_view_and_csrf(client):
    user_id = _login(client)
    item_id = _feed_item()
    page = client.get("/dashboard/opportunities")
    token = _csrf(page.text)

    assert client.post(f"/dashboard/opportunities/{item_id}/save", data={"csrf_token": "bad"}).status_code == 403
    assert client.post(
        f"/dashboard/opportunities/{item_id}/save",
        data={"csrf_token": token},
        follow_redirects=False,
    ).status_code == 303
    page = client.get("/dashboard/opportunities")
    token = _csrf(page.text)
    viewed = client.post(
        f"/dashboard/opportunities/{item_id}/view",
        data={"csrf_token": token, "action": "repository_opened"},
    )
    assert viewed.json() == {"ok": True}
    page = client.get("/dashboard/opportunities")
    token = _csrf(page.text)
    assert client.post(
        f"/dashboard/opportunities/{item_id}/dismiss",
        data={"csrf_token": token},
        follow_redirects=False,
    ).status_code == 303

    from database import SessionLocal
    from models import UserOpportunityInteraction

    db = SessionLocal()
    actions = {
        value for (value,) in db.query(UserOpportunityInteraction.action).filter(
            UserOpportunityInteraction.user_id == user_id
        ).all()
    }
    db.close()
    assert {"saved", "dismissed", "viewed", "repository_opened"} <= actions
    dismissed_page = client.get("/dashboard/opportunities").text
    assert "daily-opportunity-card" not in dismissed_page
    assert "Saved Opportunities" in dismissed_page


def test_interactions_are_isolated_between_users(client):
    first_user = _login(client, email="first@example.com")
    item_id = _feed_item()
    page = client.get("/dashboard/opportunities")
    client.post(f"/dashboard/opportunities/{item_id}/save", data={"csrf_token": _csrf(page.text)})
    client.get("/logout")
    second_user = _login(client, email="second@example.com")

    from database import SessionLocal
    from models import UserOpportunityInteraction

    db = SessionLocal()
    assert db.query(UserOpportunityInteraction).filter(UserOpportunityInteraction.user_id == first_user).count() == 1
    assert db.query(UserOpportunityInteraction).filter(UserOpportunityInteraction.user_id == second_user).count() == 0
    db.close()


def test_refresh_route_is_rate_limited_and_tracks_events(client, monkeypatch):
    _login(client)
    page = client.get("/dashboard/opportunities")
    token = _csrf(page.text)
    monkeypatch.setattr("app.refresh_opportunity_feed", lambda force=False: {"status": "refreshed", "updated": 3, "failed": 0})
    first = client.post(
        "/dashboard/opportunities/refresh",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert first.headers["location"].endswith("refresh=refreshed")
    page = client.get("/dashboard/opportunities")
    second = client.post(
        "/dashboard/opportunities/refresh",
        data={"csrf_token": _csrf(page.text)},
        follow_redirects=False,
    )
    assert second.headers["location"].endswith("refresh=rate_limited")

    from database import SessionLocal
    from models import Event

    db = SessionLocal()
    names = {name for (name,) in db.query(Event.event_name).all()}
    db.close()
    assert {"opportunity_refresh_requested", "opportunity_refresh_completed"} <= names


def test_service_reuses_fresh_cache_and_refreshes_expired_item(client, monkeypatch):
    import opportunity_service
    from database import SessionLocal
    from models import OpportunityFeedItem

    for index in range(5):
        _feed_item(f"example/fresh-{index}")
    monkeypatch.setattr(opportunity_service, "build_candidate_pool", lambda db: pytest.fail("fresh cache should be reused"))
    assert opportunity_service.refresh_opportunity_feed()["status"] == "cache_hit"

    db = SessionLocal()
    db.query(OpportunityFeedItem).update({"expires_at": datetime.now(timezone.utc) - timedelta(hours=1)})
    db.commit()
    db.close()
    monkeypatch.setattr(
        opportunity_service,
        "build_candidate_pool",
        lambda db: [{"repository_full_name": "example/fresh-0", "repository_url": "https://github.com/example/fresh-0"}],
    )
    monkeypatch.setattr(opportunity_service, "build_analysis_result", lambda url: _analysis_result("example/fresh-0", 93))
    result = opportunity_service.refresh_opportunity_feed(force=True)
    assert result["status"] == "refreshed"
    db = SessionLocal()
    assert db.query(OpportunityFeedItem).filter(OpportunityFeedItem.repository_full_name == "example/fresh-0").one().radar_score == 93
    db.close()


def test_failed_refresh_preserves_stale_cache(client, monkeypatch):
    import opportunity_service

    _feed_item(expires_delta=timedelta(hours=-1))
    monkeypatch.setattr(
        opportunity_service,
        "build_candidate_pool",
        lambda db: [{"repository_full_name": "example/repo", "repository_url": "https://github.com/example/repo"}],
    )
    monkeypatch.setattr(opportunity_service, "build_analysis_result", lambda url: (_ for _ in ()).throw(Exception("rate limit secret")))
    result = opportunity_service.refresh_opportunity_feed(force=True)
    assert result == {"status": "stale", "updated": 0, "failed": 1}
    assert "example/repo" in client.get("/today").text


def test_missing_best_issue_renders_gracefully(client):
    _feed_item(best_issue=False)
    response = client.get("/today")
    assert "ranked public issue was not available" in response.text
    assert "Open issue" not in response.text


def test_developer_profile_uses_shared_recommendation_service(client):
    user_id = _login(client)
    _profile(user_id)
    _feed_item()
    response = client.get(f"/developer/developer-{user_id}")
    assert response.status_code == 200
    assert "Recommended Next Opportunities" in response.text
    assert "example/repo" in response.text
    assert "profile match signal" in response.text


def test_opportunity_events_and_ctas_are_recorded(client):
    client.get("/today")
    client.get("/developer?source=today")
    client.get("/pricing?source=opportunity")

    from database import SessionLocal
    from models import Event

    db = SessionLocal()
    names = {name for (name,) in db.query(Event.event_name).all()}
    db.close()
    assert {"public_today_viewed", "opportunity_profile_cta_clicked", "opportunity_upgrade_clicked"} <= names


def test_cron_script_returns_safe_status(monkeypatch):
    from scripts import refresh_opportunity_feed as script

    monkeypatch.setattr(script, "refresh_opportunity_feed", lambda force=True: {"status": "refreshed", "updated": 4, "failed": 0})
    assert script.main() == 0
