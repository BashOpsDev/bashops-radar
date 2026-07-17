import json
import re
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _csrf(html):
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not match:
        match = re.search(r'id="(?:todayShareCsrf|jobsEventCsrf)" value="([^"]+)"', html)
    assert match
    return match.group(1)


def _feed_item(full_name="example/repo", score=91, language="Python", categories=None):
    from database import SessionLocal
    from models import OpportunityFeedItem

    now = datetime.now(timezone.utc)
    owner, name = full_name.split("/", 1)
    item = OpportunityFeedItem(
        repository_full_name=full_name,
        repository_url=f"https://github.com/{full_name}",
        repository_owner=owner,
        repository_name=name,
        description="An active repository",
        primary_language=language,
        categories=categories or ["Backend APIs", "Good First Issue"],
        topics=["api", "testing"],
        radar_score=score,
        decision="YES - strong Proof-of-Work target",
        best_issue_number=7,
        best_issue_title="Fix API timeout",
        best_issue_url=f"https://github.com/{full_name}/issues/7",
        difficulty="Medium",
        merge_probability="High",
        maintainer_activity_signal="Maintenance activity signal: High",
        recent_activity_signal="Repository pushed 2 days ago",
        commercial_signal="Organization-owned metadata is available.",
        paid_sprint_signal="Potential paid-sprint signal: strong evidence is present",
        public_reason="Recent activity and a ranked issue are available.",
        source_snapshot={"changes": ["New to today's bounded opportunity pool"]},
        analyzed_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=12),
        first_seen_at=now - timedelta(hours=1),
        last_seen_at=now - timedelta(hours=1),
        is_active=True,
    )
    db = SessionLocal()
    db.add(item)
    db.commit()
    item_id = item.id
    db.close()
    return item_id


def _profile(*, public=True, claimed=True, display_name="Dev User"):
    from database import SessionLocal
    from models import DeveloperProfile

    now = datetime.now(timezone.utc)
    profile = DeveloperProfile(
        github_username="dev-user",
        github_user_id="4242",
        display_name=display_name,
        avatar_url="",
        bio="Backend contributor",
        public_location="",
        profile_url="https://github.com/dev-user",
        profile_data={
            "repositories_contributed_to": 4,
            "public_pull_requests_found": 6,
            "merged_pull_requests_found": 5,
            "public_issues_found": 3,
        },
        strength_data={
            "categories": [{"label": "Backend APIs", "count": 4}],
            "languages": [{"label": "Python", "count": 5}],
            "narrative": "Public contribution evidence",
            "portfolio_summary": "Public contribution evidence",
        },
        contribution_data=[],
        analyzed_at=now,
        expires_at=now + timedelta(hours=24),
        is_claimed=claimed,
        is_public=public,
        public_slug="dev-user",
    )
    db = SessionLocal()
    db.add(profile)
    db.commit()
    db.close()


def test_profile_cards_require_published_profile_and_escape_svg(client):
    _profile(public=False, display_name='<script>alert("x")</script>')
    assert client.get("/developer/dev-user/card").status_code == 404

    from database import SessionLocal
    from models import DeveloperProfile

    db = SessionLocal()
    profile = db.query(DeveloperProfile).one()
    profile.is_public = True
    db.commit()
    db.close()

    response = client.get("/developer/dev-user/card")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "&lt;script&gt;" in response.text
    assert "<script>" not in response.text
    assert "bashops.site/developer/dev-user" in response.text
    assert "Generated from public GitHub activity." in response.text
    downloaded = client.get("/developer/dev-user/card?download=true")
    assert "attachment" in downloaded.headers["content-disposition"]
    from models import Event

    db = SessionLocal()
    event_names = {event.event_name for event in db.query(Event).all()}
    assert {"share_card_generated", "share_download"} <= event_names
    db.close()


def test_recommendation_and_today_cards_use_cached_feed_only(client, monkeypatch):
    import app as app_module

    _profile()
    _feed_item()
    monkeypatch.setattr(app_module, "refresh_opportunity_feed", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("refresh not allowed")))
    recommendation = client.get("/developer/dev-user/recommendation-card")
    today = client.get("/today/card")
    assert recommendation.status_code == 200
    assert today.status_code == 200
    assert "example/repo" in recommendation.text
    assert "YOUR NEXT OSS OPPORTUNITY" in recommendation.text
    assert "bashops.site/developer/dev-user" in recommendation.text
    assert "91/100" in today.text
    assert "PAID SPRINT POTENTIAL" in today.text


def test_cached_repository_summary_never_analyzes_and_has_scoped_cors(client, monkeypatch):
    import app as app_module

    _feed_item(full_name="Example/Repo")
    monkeypatch.setattr(app_module, "build_analysis_result", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("analysis not allowed")))
    origin = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
    response = client.get(
        "/api/public/repository-summary?owner=example&repo=repo",
        headers={"Origin": origin},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    payload = response.json()
    assert payload["available"] is True
    assert payload["radar_score"] == 91
    assert payload["best_issue"]["number"] == 7
    assert payload["contract_potential"] == "High"
    assert "High" in payload["maintainer_activity"]
    assert client.get("/api/public/repository-summary?owner=missing&repo=repo").status_code == 404
    assert client.get("/api/public/repository-summary?owner=bad/path&repo=repo").status_code == 400
    from database import SessionLocal
    from models import Event

    db = SessionLocal()
    event_names = {event.event_name for event in db.query(Event).all()}
    assert {"extension_open", "extension_repository"} <= event_names
    db.close()


def test_jobs_page_is_cache_backed_indexable_and_in_sitemap(client, monkeypatch):
    import app as app_module

    _feed_item()
    monkeypatch.setattr(app_module, "_opportunity_refresh_status", lambda: {"status": "fresh", "updated": 0, "failed": 0})
    response = client.get("/jobs")
    assert response.status_code == 200
    assert "Today's Open Source Jobs" in response.text
    assert "Best Opportunities" in response.text
    assert "Highest Paid Sprint Potential" in response.text
    assert "Fast Merge Opportunities" in response.text
    assert "Great First Contribution" in response.text
    assert "View Opportunity Analysis" in response.text
    assert "Last refreshed" in response.text
    assert "example/repo" in response.text
    assert "not confirmed jobs" in response.text
    assert '<link rel="canonical" href="http://testserver/jobs">' in response.text
    assert 'type="application/ld+json"' in response.text
    assert "/jobs" in client.get("/sitemap.xml").text


def test_category_matching_does_not_treat_maintainer_as_ai(client):
    from database import SessionLocal
    from opportunity_service import curated_opportunity_sections, load_feed_items

    _feed_item(full_name="example/maintainer", categories=["Maintainer Tools"])
    db = SessionLocal()
    sections = {section["key"]: section for section in curated_opportunity_sections(load_feed_items(db))}
    db.close()
    assert "ai" not in sections


def test_public_distribution_events_are_csrf_protected_and_allowlisted(client, monkeypatch):
    import app as app_module
    from database import SessionLocal
    from models import Event

    monkeypatch.setattr(app_module, "_opportunity_refresh_status", lambda: {"status": "fresh", "updated": 0, "failed": 0})
    page = client.get("/jobs")
    token = _csrf(page.text)
    assert client.post("/public/event", data={"action": "share_copy", "surface": "jobs", "csrf_token": "bad"}).status_code == 403
    assert client.post("/public/event", data={"action": "unknown", "surface": "jobs", "csrf_token": token}).status_code == 400
    tracked = client.post(
        "/public/event",
        data={"action": "jobs_category", "surface": "jobs", "detail": "python", "csrf_token": token},
    )
    assert tracked.status_code == 200
    db = SessionLocal()
    assert db.query(Event).filter(Event.event_name == "jobs_category").count() == 1
    db.close()


def test_extension_manifest_is_minimal_and_has_no_background_tracking():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "chrome-extension" / "manifest.json").read_text(encoding="utf-8"))
    popup = (root / "chrome-extension" / "popup.js").read_text(encoding="utf-8")
    popup_html = (root / "chrome-extension" / "popup.html").read_text(encoding="utf-8")
    assert manifest["manifest_version"] == 3
    assert manifest["host_permissions"] == ["https://github.com/*", "https://bashops.site/*"]
    assert "permissions" not in manifest
    assert "background" not in manifest
    assert "/api/public/repository-summary" in popup
    assert "Maintainers active" in popup
    assert "Open Today's Opportunities" in popup_html
    assert "localStorage" not in popup


def test_railway_runs_migrations_before_deploy_and_laptop_nav_is_collapsed():
    root = Path(__file__).resolve().parents[1]
    railway_config = tomllib.loads((root / "railway.toml").read_text(encoding="utf-8"))
    stylesheet = (root / "static" / "style.css").read_text(encoding="utf-8")
    assert railway_config["deploy"]["preDeployCommand"] == ["alembic upgrade head"]
    assert "@media (max-width: 1420px)" in stylesheet
    assert "@media (min-width: 1421px)" in stylesheet
    assert "@media (min-width: 861px) and (max-width: 1420px)" in stylesheet


def test_extension_clickthrough_sources_are_tracked(client, monkeypatch):
    import app as app_module
    from database import SessionLocal
    from models import Event

    monkeypatch.setattr(app_module, "_opportunity_refresh_status", lambda: {"status": "fresh", "updated": 0, "failed": 0})
    client.get("/today?source=extension")
    home = client.get("/?source=extension-analyze&repo_url=https://github.com/example/repo")
    assert 'value="https://github.com/example/repo"' in home.text
    db = SessionLocal()
    event_names = {event.event_name for event in db.query(Event).all()}
    assert {"extension_open_radar", "extension_analyze_click"} <= event_names
    db.close()
