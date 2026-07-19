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
    assert "Commercial open-source projects showing stronger business" in response.text
    assert "Why ranked here" in response.text
    assert "Category fit" in response.text
    assert "Evidence used" in response.text
    assert "View Opportunity Analysis" in response.text
    assert "Last refreshed" in response.text
    assert "example/repo" in response.text
    assert "not confirmed jobs" in response.text
    assert '<link rel="canonical" href="http://testserver/jobs">' in response.text
    assert 'type="application/ld+json"' in response.text
    assert 'data-public-event="jobs_category_selected"' in response.text
    assert 'data-jobs-card' in response.text
    assert "const emitted = new Set()" in response.text
    assert "observer.unobserve(element)" in response.text
    assert "/jobs" in client.get("/sitemap.xml").text


def test_category_matching_does_not_treat_maintainer_as_ai(client):
    from database import SessionLocal
    from opportunity_service import curated_opportunity_sections, load_feed_items

    _feed_item(full_name="example/maintainer", categories=["Maintainer Tools"])
    db = SessionLocal()
    sections = {section["key"]: section for section in curated_opportunity_sections(load_feed_items(db))}
    db.close()
    assert sections["ai"]["items"] == []


def test_curated_categories_use_distinct_ranking_signals():
    from database import SessionLocal
    from models import OpportunityFeedItem
    from opportunity_service import curated_opportunity_sections, load_feed_items

    high_score_id = _feed_item(full_name="example/commercial", score=96)
    fast_issue_id = _feed_item(full_name="example/fast-fix", score=84)
    personal_id = _feed_item(full_name="student/demo", score=70)
    db = SessionLocal()
    high_score = db.query(OpportunityFeedItem).filter(OpportunityFeedItem.id == high_score_id).one()
    high_score.merge_probability = "Medium"
    high_score.difficulty = "Medium"
    fast_issue = db.query(OpportunityFeedItem).filter(OpportunityFeedItem.id == fast_issue_id).one()
    fast_issue.merge_probability = "High"
    fast_issue.difficulty = "Low"
    fast_issue.categories = ["Good First Issue"]
    personal = db.query(OpportunityFeedItem).filter(OpportunityFeedItem.id == personal_id).one()
    personal.commercial_signal = "Repository metadata alone does not show a commercial signal."
    personal.paid_sprint_signal = "Paid-sprint potential not established from available metadata"
    personal.maintainer_activity_signal = "Maintenance activity signal: High"
    personal.merge_probability = "High"
    personal.difficulty = "Low"
    personal.categories = ["Good First Issue"]
    db.commit()

    sections = {section["key"]: section for section in curated_opportunity_sections(load_feed_items(db))}
    db.close()

    assert sections["trending"]["items"][0].repository_full_name == "example/commercial"
    assert sections["fast-merge"]["items"][0].repository_full_name == "example/fast-fix"
    assert sections["great-first-contribution"]["items"][0].repository_full_name == "example/fast-fix"
    assert all(item.repository_full_name != "student/demo" for item in sections["paid-sprint"]["items"])
    assert all(item.repository_full_name != "student/demo" for item in sections["founder-friendly"]["items"])
    assert any(item.repository_full_name == "student/demo" for item in sections["fast-merge"]["items"])
    assert any(item.repository_full_name == "student/demo" for item in sections["great-first-contribution"]["items"])


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
        data={
            "action": "jobs_repository_clicked",
            "surface": "jobs",
            "category": "python",
            "repository": "example/repo",
            "position": "2",
            "csrf_token": token,
        },
    )
    assert tracked.status_code == 200
    db = SessionLocal()
    event = db.query(Event).filter(Event.event_name == "jobs_repository_clicked").one()
    metadata = json.loads(event.metadata_json)
    assert metadata == {
        "surface": "jobs",
        "detail": "",
        "category": "python",
        "repository": "example/repo",
        "position": 2,
        "authenticated": False,
    }
    db.close()


def test_jobs_footer_uses_existing_trust_and_documentation_destinations(client):
    response = client.get("/jobs")
    assert response.status_code == 200
    assert "Built using deterministic repository analysis" in response.text
    assert 'href="/methodology#evidence-engine"' in response.text
    assert 'href="/methodology"' in response.text
    assert 'href="/docs"' in response.text
    assert 'href="https://github.com/BashOpsDev/bashops-radar"' in response.text
    assert 'href="/privacy"' in response.text
    assert client.get("/methodology").status_code == 200
    assert client.get("/privacy").status_code == 200
    assert client.get("/docs").status_code == 200


def test_jobs_empty_and_database_failure_states_are_safe(client, monkeypatch):
    import app as app_module
    from sqlalchemy.exc import SQLAlchemyError

    empty = client.get("/jobs")
    assert empty.status_code == 200
    assert "opportunity cache is being refreshed" in empty.text

    monkeypatch.setattr(
        app_module,
        "load_feed_items",
        lambda _db: (_ for _ in ()).throw(SQLAlchemyError("private database detail")),
    )
    failed = client.get("/jobs")
    assert failed.status_code == 200
    assert "public opportunity feed is temporarily unavailable" in failed.text
    assert "private database detail" not in failed.text


def test_jobs_category_engagement_is_available_to_existing_admin_summary(client):
    import app as app_module
    from database import SessionLocal
    from models import Event

    page = client.get("/jobs")
    token = _csrf(page.text)
    for action in ("jobs_category_viewed", "jobs_category_selected", "jobs_repository_clicked", "jobs_issue_opened", "jobs_analysis_started"):
        response = client.post(
            "/public/event",
            data={
                "action": action,
                "surface": "jobs",
                "category": "backend",
                "repository": "example/repo",
                "position": "1",
                "csrf_token": token,
            },
        )
        assert response.status_code == 200

    summary = app_module.admin_event_summary()
    backend = next(item for item in summary["jobs_categories"] if item["category"] == "backend")
    assert backend == {
        "category": "backend",
        "views": 1,
        "selections": 1,
        "clicks": 1,
        "analyses": 1,
        "issues": 1,
        "click_through_rate": 100.0,
    }
    assert summary["jobs_positions"][0] == {"position": 1, "clicks": 1}

    db = SessionLocal()
    assert db.query(Event).filter(Event.event_name == "jobs_page_viewed").count() == 1
    assert all("user@example.com" not in (event.metadata_json or "") for event in db.query(Event).all())
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
