import os
import json
import re
import secrets
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
import requests

from database import SessionLocal
from models import Event, MaintainerAnalysis, Target, User
from auth import (
    hash_password,
    verify_password,
    generate_csrf_token,
    verify_csrf_token,
)
from analytics import (
    analytics_summary,
    track_analysis,
    update_pipeline_status,
    generate_pitch,
    save_pitch,
    daily_analysis_count,
    lifetime_analysis_count,
)
import paddle_billing
import email_utils
from analysis_service import build_analysis_result, to_public_api_payload
from discovery_service import DiscoveryError, category_options, discover_opportunities
from maintainer_schemas import MaintainerReport
from maintainer_service import ANALYSIS_VERSION as MAINTAINER_ANALYSIS_VERSION
from maintainer_service import MaintainerServiceError, build_maintainer_report, parse_repository_url
import config

try:
    import google.generativeai as genai
except Exception:
    genai = None


# SECRET_KEY is required, not defaulted. A hardcoded fallback would mean
# every session cookie (and CSRF token) is forgeable if the env var is ever
# missing on deploy. Fail loudly at startup instead of failing silently in
# production.
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY environment variable is not set. Set it before starting "
        "the app (e.g. `SECRET_KEY=$(openssl rand -hex 32)`). Refusing to "
        "start with an insecure default."
    )

app = FastAPI(title="BashOps Radar")

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
BETA_FILE = Path("beta_signups.csv")
FREE_ANALYSIS_LIMIT = 2
ANONYMOUS_ANALYSIS_LIMIT = 1
MAINTAINER_PENDING_PARTIAL_SESSION_KEY = "maintainer_pending_partial"
EMAIL_VERIFICATION_MAX_AGE_SECONDS = 24 * 60 * 60
PASSWORD_RESET_MAX_AGE_SECONDS = 60 * 60

if GEMINI_API_KEY and genai:
    genai.configure(api_key=GEMINI_API_KEY)


def clean_ai_summary_text(text: str) -> str:
    """Keep model output readable in the HTML card without touching GitHub data."""
    if not text:
        return "AI summary temporarily unavailable."

    cleaned = text.strip()
    cleaned = re.sub(r"^\s*#{1,6}\s*", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*[-*]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.*?)__", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() or "AI summary temporarily unavailable."


def generate_ai_summary(repo_full_name, repo, best_issue, repo_score, angle):
    if not GEMINI_API_KEY or not genai:
        return {
            "text": "AI summary is not enabled yet. Add GEMINI_API_KEY to enable Gemini analysis.",
            "status": "unavailable",
        }

    issue_text = "No best issue found."
    if best_issue:
        issue_text = (
            f"#{best_issue['number']} - {best_issue['title']} "
            f"({best_issue['type']}, score {best_issue['score']}/100)"
        )

    prompt = f"""
You are BashOps Radar, an AI opportunity analyst for developers.

Analyze this GitHub repository as a proof-of-work opportunity.

Repository: {repo_full_name}
Description: {repo.get("description")}
Stars: {repo.get("stargazers_count")}
Forks: {repo.get("forks_count")}
Open Issues: {repo.get("open_issues_count")}
Opportunity Score: {repo_score}/100
Best Issue: {issue_text}
Proof-of-Work Angle: {angle}

Write a concise plain-text analysis that covers:
Why this repo is worth or not worth contributing to.
Why the best issue is a good first target.
How the developer should approach the PR.
Whether this could lead to a paid sprint.

Keep it practical, direct, and under 180 words.
Do not use Markdown, headings, bold markers, bullets, numbered lists, or code formatting.
"""

    try:
        model = genai.GenerativeModel("gemini-2.5-flash-lite")
        response = model.generate_content(prompt)

        return {
            "text": clean_ai_summary_text(response.text) if response.text else "AI summary temporarily unavailable.",
            "status": "available",
        }

    except Exception as e:
        error = str(e).lower()

        if "429" in error or "quota" in error or "rate limit" in error:
            return {
                "text": "Gemini free-tier quota reached. Core repository analysis completed successfully.",
                "status": "unavailable",
            }

        return {
            "text": "AI summary temporarily unavailable. Core repository analysis completed successfully.",
            "status": "unavailable",
        }


def get_current_user(request: Request):
    user_id = request.session.get("user_id")

    if not user_id:
        return None

    db: Session = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id).first()
    finally:
        db.close()


def is_admin(user) -> bool:
    return bool(user and user.email and user.email.strip().lower() in config.ADMIN_EMAILS)


def has_owner_pro_override(user) -> bool:
    if not user or not user.email:
        return False
    email = user.email.strip().lower()
    return email == "bashops1@gmail.com" or email in config.ADMIN_EMAILS


def has_pro_access(user) -> bool:
    return bool(user and (user.plan == "pro" or has_owner_pro_override(user)))


def require_admin_or_redirect(request: Request, current_user):
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    if not is_admin(current_user):
        return HTMLResponse(
            "Admin access required. Confirm this account email is listed in ADMIN_EMAILS.",
            status_code=403,
        )
    return None


def user_context(request: Request, current_user=None) -> dict:
    """Standard current_user / is_admin pair every template needs for the
    navbar to render the right links."""
    if current_user is None:
        current_user = get_current_user(request)
    pro_access = has_pro_access(current_user)
    return {
        "current_user": current_user,
        "is_admin": is_admin(current_user),
        "has_pro_access": pro_access,
        "effective_plan": "pro" if pro_access else "free",
    }


def csrf_context(request: Request) -> dict:
    """Every template that renders a POST form should merge this in so the
    form can include the hidden csrf_token field."""
    token = generate_csrf_token()
    request.session["csrf_token"] = token
    return {"csrf_token": token}


def check_csrf(request: Request, csrf_token: str) -> bool:
    session_token = request.session.get("csrf_token", "")
    # The token must both verify its own signature/expiry AND match the one
    # issued for this session, so a stolen token from a different session
    # can't be replayed.
    return verify_csrf_token(csrf_token) and csrf_token == session_token


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def token_age_seconds(sent_at) -> float:
    if not sent_at:
        return float("inf")
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    return (now_utc() - sent_at).total_seconds()


def validate_password_strength(password: str) -> Optional[str]:
    if len(password or "") < 8:
        return "Password must be at least 8 characters long."
    if not any(char.isupper() for char in password):
        return "Password must include at least one uppercase letter."
    if not any(char.islower() for char in password):
        return "Password must include at least one lowercase letter."
    if not any(char.isdigit() for char in password):
        return "Password must include at least one number."
    return None


def new_token() -> str:
    return secrets.token_urlsafe(48)


def verification_link(token: str) -> str:
    return f"{config.SITE_URL}/verify-email?token={token}"


def reset_link(token: str) -> str:
    return f"{config.SITE_URL}/reset-password?token={token}"


def render_login(
    request: Request,
    error: Optional[str] = None,
    joined: bool = False,
    registered: bool = False,
    verified: bool = False,
    reset: bool = False,
):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "joined": joined,
            "registered": registered,
            "verified": verified,
            "reset": reset,
            "error": error,
            "github_oauth_configured": config.github_oauth_configured,
            **user_context(request),
            **csrf_context(request),
        },
    )


def render_register(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "error": error,
            "github_oauth_configured": config.github_oauth_configured,
            **user_context(request),
            **csrf_context(request),
        },
    )


def track_event(request: Request, event_name: str, user=None, metadata=None) -> None:
    try:
        db: Session = SessionLocal()
        try:
            event = Event(
                user_id=user.id if user else None,
                event_name=event_name,
                page=str(request.url.path)[:500],
                referrer=(request.headers.get("referer") or "")[:500],
                user_agent=(request.headers.get("user-agent") or "")[:500],
                metadata_json=json.dumps(metadata or {}, default=str),
            )
            db.add(event)
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        print(f"[event tracking failed] {event_name}: {exc!r}")


def anonymous_website_analysis_used(request: Request, ip: str) -> bool:
    """
    Website-only anonymous trial guard. The session flag is the primary signal;
    the existing anonymous Target/IP count remains a soft fallback for the
    current network without adding fingerprinting or new storage.
    """
    if request.session.get("anonymous_analysis_used"):
        return True
    return daily_analysis_count(ip=ip) >= ANONYMOUS_ANALYSIS_LIMIT


def free_account_analysis_count(user) -> int:
    """Free account quota is derived from successful Target rows."""
    if not user:
        return 0
    return lifetime_analysis_count(user.id)


def require_maintainer_enabled() -> None:
    if not config.MAINTAINER_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")


def has_maintainer_access(user) -> bool:
    return bool(
        user
        and (
            has_owner_pro_override(user)
            or bool(getattr(user, "maintainer_pilot_access", False))
        )
    )


def maintainer_trial_used(request: Request, user, ip: str) -> bool:
    if has_maintainer_access(user):
        return False
    if not user and request.session.get("maintainer_trial_used"):
        return True

    db: Session = SessionLocal()
    try:
        query = db.query(MaintainerAnalysis).filter(
            MaintainerAnalysis.status == "completed",
            MaintainerAnalysis.is_partial.is_(False),
        )
        if user:
            query = query.filter(MaintainerAnalysis.user_id == user.id)
        else:
            query = query.filter(
                MaintainerAnalysis.user_id.is_(None),
                MaintainerAnalysis.ip_address == ip,
            )
        return query.count() >= 1
    finally:
        db.close()


def maintainer_pending_partial_repository(request: Request, user) -> Optional[str]:
    """Return the Free user's session-bound partial repository, if any."""
    if not user or has_maintainer_access(user):
        return None
    pending = request.session.get(MAINTAINER_PENDING_PARTIAL_SESSION_KEY)
    if not isinstance(pending, dict) or pending.get("user_id") != user.id:
        return None
    repository = pending.get("repository")
    return repository if isinstance(repository, str) and repository else None


def maintainer_plan_context(user) -> str:
    if has_owner_pro_override(user):
        return "owner_admin"
    if user and getattr(user, "maintainer_pilot_access", False):
        return "pilot"
    return "registered_trial" if user else "anonymous_trial"


def maintainer_template_context(request: Request, current_user=None) -> dict:
    if current_user is None:
        current_user = get_current_user(request)
    return {
        **user_context(request, current_user),
        "maintainer_access": has_maintainer_access(current_user),
        "pilot_price": config.MAINTAINER_PILOT_PRICE_USD,
        "site_url": config.SITE_URL,
    }


@app.exception_handler(StarletteHTTPException)
async def not_found_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return templates.TemplateResponse(
            request=request,
            name="404.html",
            context={**user_context(request)},
            status_code=404,
        )
    # Any other raised HTTPException (403, 401, etc.) keeps FastAPI's
    # default plain response — only 404 gets the branded page, so real
    # status codes used by API-style clients aren't silently reshaped.
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def server_error_handler(request: Request, exc: Exception):
    # Last-resort safety net: if a route raises anything unhandled, log it
    # server-side and show the branded 500 page instead of a raw traceback
    # or FastAPI's default plain-text error leaking internals to the user.
    print(f"[unhandled error] {request.method} {request.url.path}: {exc!r}")
    return templates.TemplateResponse(
        request=request,
        name="500.html",
        context={**user_context(request)},
        status_code=500,
    )


@app.get("/robots.txt")
def robots_txt():
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /dashboard",
        "Disallow: /pipeline",
        "Disallow: /admin/",
        "Disallow: /billing/",
        "Disallow: /maintainer/dashboard",
        "Disallow: /maintainer/report/",
        "Disallow: /export-pipeline",
        f"Sitemap: {config.SITE_URL}/sitemap.xml",
    ]
    return Response(content="\n".join(lines) + "\n", media_type="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    # Only the public, indexable marketing pages — the app pages behind
    # login are excluded via robots.txt above and noindex tags on the
    # pages themselves.
    urls = [
        "/",
        "/pricing",
        "/tools/github-opportunity-score",
        "/tools/best-first-issue-finder",
        "/login",
        "/register",
        "/terms",
        "/privacy",
        "/refund",
        "/contact",
    ]
    if config.MAINTAINER_ENABLED:
        urls.extend(["/maintainer", "/maintainer/pricing"])
    body = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path in urls:
        body.append(f"<url><loc>{config.SITE_URL}{path}</loc></url>")
    body.append("</urlset>")
    return Response(content="\n".join(body), media_type="application/xml")


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    current_user = get_current_user(request)
    track_event(request, "landing_view", user=current_user)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "result": None,
            "error": None,
            "limit_reached": False,
            "site_url": config.SITE_URL,
            "pro_price": config.PRO_PRICE_USD,
            **user_context(request, current_user),
            **csrf_context(request),
        },
    )


@app.get("/export-pipeline")
def export_pipeline(request: Request):
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    import csv
    import io

    summary = analytics_summary(user_id=current_user.id)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["repo", "score", "status", "best_issue", "difficulty", "merge_probability", "estimated_time", "created_at"])
    for row in summary["rows"]:
        writer.writerow([
            row["repo"], row["score"], row["status"], row["best_issue"],
            row["difficulty"], row["merge_probability"], row["estimated_time"], row["pretty_time"],
        ])

    from fastapi.responses import Response
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=bashops_pipeline.csv"},
    )


@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    current_user = get_current_user(request)
    track_event(request, "pricing_view", user=current_user)
    return templates.TemplateResponse(
        request=request,
        name="pricing.html",
        context={
            "result": None,
            "error": None,
            "limit_reached": False,
            "pro_price": config.PRO_PRICE_USD,
            "billing_error": None,
            "site_url": config.SITE_URL,
            **user_context(request, current_user),
        },
    )


def render_maintainer_landing(
    request: Request,
    current_user=None,
    error: Optional[str] = None,
    trial_blocked: bool = False,
    trial_block_reason: Optional[str] = None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request=request,
        name="maintainer/landing.html",
        context={
            "error": error,
            "trial_blocked": trial_blocked,
            "trial_block_reason": trial_block_reason,
            **maintainer_template_context(request, current_user),
            **csrf_context(request),
        },
        status_code=status_code,
    )


@app.get("/maintainer", response_class=HTMLResponse)
def maintainer_landing(request: Request):
    require_maintainer_enabled()
    current_user = get_current_user(request)
    track_event(request, "maintainer_page_viewed", user=current_user)
    return render_maintainer_landing(request, current_user)


@app.post("/maintainer/analyze", response_class=HTMLResponse)
def maintainer_analyze(
    request: Request,
    repo_url: str = Form(...),
    csrf_token: str = Form(""),
):
    require_maintainer_enabled()
    current_user = get_current_user(request)
    ip = request.client.host if request.client else "unknown"

    if not check_csrf(request, csrf_token):
        return render_maintainer_landing(
            request,
            current_user,
            error="Your session expired. Please try again.",
            status_code=400,
        )

    if maintainer_trial_used(request, current_user, ip):
        track_event(
            request,
            "maintainer_trial_blocked",
            user=current_user,
            metadata={"access": maintainer_plan_context(current_user)},
        )
        return render_maintainer_landing(
            request,
            current_user,
            trial_blocked=True,
            status_code=429,
        )

    pending_repository = maintainer_pending_partial_repository(request, current_user)
    if pending_repository:
        try:
            owner, repository, _ = parse_repository_url(repo_url)
        except MaintainerServiceError as exc:
            return render_maintainer_landing(
                request,
                current_user,
                error=exc.public_message,
                status_code=400,
            )
        if f"{owner}/{repository}".casefold() != pending_repository:
            track_event(
                request,
                "maintainer_trial_blocked",
                user=current_user,
                metadata={"access": maintainer_plan_context(current_user), "reason": "pending_partial"},
            )
            return render_maintainer_landing(
                request,
                current_user,
                trial_blocked=True,
                trial_block_reason="pending_partial",
                status_code=429,
            )

    track_event(request, "maintainer_analysis_started", user=current_user, metadata={"repo_url": repo_url})
    try:
        outcome = build_maintainer_report(repo_url)
    except MaintainerServiceError as exc:
        track_event(
            request,
            "maintainer_analysis_failed",
            user=current_user,
            metadata={"error_code": exc.error_code},
        )
        return render_maintainer_landing(
            request,
            current_user,
            error=exc.public_message,
            status_code=400,
        )
    except Exception as exc:
        print(f"[/maintainer/analyze error] {exc!r}")
        track_event(
            request,
            "maintainer_analysis_failed",
            user=current_user,
            metadata={"error_code": "unexpected_error"},
        )
        return render_maintainer_landing(
            request,
            current_user,
            error="The report could not be completed. Please try again.",
            status_code=500,
        )

    report = MaintainerReport.model_validate(outcome["report"]).model_dump(mode="json")
    if outcome["is_partial"]:
        if not current_user:
            request.session["maintainer_trial_used"] = True
            report_notice = (
                "AI-assisted review was unavailable, so this deterministic partial report was generated. "
                "Your free Maintainer preview has been used. Create an account to continue evaluating "
                "repository issue queues."
            )
        elif has_maintainer_access(current_user):
            report_notice = "AI-assisted review was unavailable, so this deterministic partial report was generated."
        else:
            owner, repository, _ = parse_repository_url(repo_url)
            request.session[MAINTAINER_PENDING_PARTIAL_SESSION_KEY] = {
                "user_id": current_user.id,
                "repository": f"{owner}/{repository}".casefold(),
            }
            report_notice = (
                "The AI-assisted report was temporarily unavailable. You can retry this repository "
                "without using your complete-report trial."
            )
        track_event(
            request,
            "maintainer_analysis_failed",
            user=current_user,
            metadata={"error_code": outcome["error_code"], "partial": True},
        )
        return templates.TemplateResponse(
            request=request,
            name="maintainer/report.html",
            context={
                "report": report,
                "analysis": None,
                "report_notice": report_notice,
                **maintainer_template_context(request, current_user),
            },
        )

    db: Session = SessionLocal()
    try:
        analysis = MaintainerAnalysis(
            user_id=current_user.id if current_user else None,
            repository_full_name=report["repository"]["full_name"],
            repository_url=report["repository"]["url"],
            status="completed",
            analyzed_issue_count=report["issues_reviewed"],
            report_json=json.dumps(report, ensure_ascii=True),
            is_partial=False,
            error_code=None,
            plan_context=maintainer_plan_context(current_user),
            analysis_version=MAINTAINER_ANALYSIS_VERSION,
            ip_address=ip,
            completed_at=now_utc(),
        )
        db.add(analysis)
        db.commit()
        db.refresh(analysis)
        analysis_id = analysis.id
    except Exception as exc:
        db.rollback()
        print(f"[/maintainer/analyze persistence error] {exc!r}")
        track_event(
            request,
            "maintainer_analysis_failed",
            user=current_user,
            metadata={"error_code": "persistence_failed"},
        )
        return render_maintainer_landing(
            request,
            current_user,
            error="The report could not be saved, so your trial was not consumed. Please try again.",
            status_code=500,
        )
    finally:
        db.close()

    if not current_user:
        request.session["maintainer_trial_used"] = True
        report_notice = (
            "Your free Maintainer preview has been used. Create an account to continue evaluating "
            "repository issue queues."
        )
    else:
        pending = request.session.get(MAINTAINER_PENDING_PARTIAL_SESSION_KEY)
        if isinstance(pending, dict) and pending.get("user_id") == current_user.id:
            request.session.pop(MAINTAINER_PENDING_PARTIAL_SESSION_KEY, None)
        report_notice = None

    track_event(
        request,
        "maintainer_analysis_completed",
        user=current_user,
        metadata={"analysis_id": analysis_id, "repository": report["repository"]["full_name"]},
    )
    track_event(request, "maintainer_trial_completed", user=current_user, metadata={"analysis_id": analysis_id})

    if current_user:
        return RedirectResponse(url=f"/maintainer/report/{analysis_id}", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="maintainer/report.html",
        context={
            "report": report,
            "analysis": None,
            "report_notice": report_notice,
            **maintainer_template_context(request, current_user),
        },
    )


@app.get("/maintainer/report/{analysis_id}", response_class=HTMLResponse)
def maintainer_report(request: Request, analysis_id: int):
    require_maintainer_enabled()
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    db: Session = SessionLocal()
    try:
        analysis = db.query(MaintainerAnalysis).filter(
            MaintainerAnalysis.id == analysis_id,
            MaintainerAnalysis.user_id == current_user.id,
            MaintainerAnalysis.status == "completed",
            MaintainerAnalysis.is_partial.is_(False),
        ).first()
        if not analysis:
            raise HTTPException(status_code=404, detail="Report not found")
        report = MaintainerReport.model_validate_json(analysis.report_json).model_dump(mode="json")
    finally:
        db.close()

    track_event(request, "maintainer_report_viewed", user=current_user, metadata={"analysis_id": analysis_id})
    return templates.TemplateResponse(
        request=request,
        name="maintainer/report.html",
        context={
            "report": report,
            "analysis": analysis,
            "report_notice": None,
            **maintainer_template_context(request, current_user),
        },
    )


@app.get("/maintainer/dashboard", response_class=HTMLResponse)
def maintainer_dashboard(request: Request):
    require_maintainer_enabled()
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    db: Session = SessionLocal()
    try:
        analyses = db.query(MaintainerAnalysis).filter(
            MaintainerAnalysis.user_id == current_user.id,
            MaintainerAnalysis.status == "completed",
            MaintainerAnalysis.is_partial.is_(False),
        ).order_by(MaintainerAnalysis.created_at.desc()).all()
        rows = []
        for analysis in analyses:
            try:
                report = MaintainerReport.model_validate_json(analysis.report_json)
            except Exception:
                continue
            rows.append({"analysis": analysis, "counts": report.counts.model_dump()})
    finally:
        db.close()

    return templates.TemplateResponse(
        request=request,
        name="maintainer/dashboard.html",
        context={
            "rows": rows,
            **maintainer_template_context(request, current_user),
            **csrf_context(request),
        },
    )


@app.get("/maintainer/pricing", response_class=HTMLResponse)
def maintainer_pricing(request: Request):
    require_maintainer_enabled()
    current_user = get_current_user(request)
    track_event(request, "maintainer_pilot_clicked", user=current_user)
    return templates.TemplateResponse(
        request=request,
        name="maintainer/pricing.html",
        context={**maintainer_template_context(request, current_user)},
    )


@app.get("/tools/github-opportunity-score", response_class=HTMLResponse)
def github_opportunity_score_tool(request: Request):
    current_user = get_current_user(request)
    # Thin SEO/tool wrapper: the form posts to /analyze so all scoring,
    # limits, tracking writes, and result rendering stay in one place.
    track_event(request, "tool_view", user=current_user, metadata={"tool": "github_opportunity_score"})
    return templates.TemplateResponse(
        request=request,
        name="tool_github_opportunity_score.html",
        context={
            "site_url": config.SITE_URL,
            "pro_price": config.PRO_PRICE_USD,
            **user_context(request, current_user),
            **csrf_context(request),
        },
    )


@app.get("/tools/best-first-issue-finder", response_class=HTMLResponse)
def best_first_issue_finder_tool(request: Request):
    current_user = get_current_user(request)
    # Thin SEO/tool wrapper: the form posts to /analyze so the existing
    # analysis engine remains the single source for best issue selection.
    track_event(request, "tool_view", user=current_user, metadata={"tool": "best_first_issue_finder"})
    return templates.TemplateResponse(
        request=request,
        name="tool_best_first_issue_finder.html",
        context={
            "site_url": config.SITE_URL,
            "pro_price": config.PRO_PRICE_USD,
            **user_context(request, current_user),
            **csrf_context(request),
        },
    )


@app.get("/discover", response_class=HTMLResponse)
def discover(request: Request):
    current_user = get_current_user(request)
    can_run_discovery = has_pro_access(current_user)
    track_event(
        request,
        "discover_view",
        user=current_user,
        metadata={"access": "pro" if can_run_discovery else "free"},
    )
    categories = category_options()
    return templates.TemplateResponse(
        request=request,
        name="discover.html",
        context={
            "categories": categories,
            "selected_category": categories[0]["value"],
            "results": [],
            "error": None,
            "can_run_discovery": can_run_discovery,
            "site_url": config.SITE_URL,
            **user_context(request, current_user),
            **csrf_context(request),
        },
    )


@app.post("/discover", response_class=HTMLResponse)
def discover_submit(
    request: Request,
    category: str = Form("python-fastapi"),
    csrf_token: str = Form(""),
):
    current_user = get_current_user(request)
    can_run_discovery = has_pro_access(current_user)
    categories = category_options()
    category_values = {item["value"] for item in categories}
    selected_category = category if category in category_values else categories[0]["value"]
    results = []
    error = None

    if not check_csrf(request, csrf_token):
        error = "Your session expired. Please try again."
    elif not can_run_discovery:
        error = "Upgrade to Pro to run Opportunity Finder."
    else:
        try:
            payload = discover_opportunities(selected_category, limit=8)
            results = payload["results"]
            track_event(
                request,
                "discover_run",
                user=current_user,
                metadata={"category": selected_category, "result_count": len(results)},
            )
            if not results:
                error = "No strong matches found for that category yet. Try another category."
        except DiscoveryError as exc:
            error = str(exc)
        except Exception as exc:
            print(f"[/discover error] {exc!r}")
            error = "Opportunity Finder is temporarily unavailable. Please try again."

    return templates.TemplateResponse(
        request=request,
        name="discover.html",
        context={
            "categories": categories,
            "selected_category": selected_category,
            "results": results,
            "error": error,
            "can_run_discovery": can_run_discovery,
            "site_url": config.SITE_URL,
            **user_context(request, current_user),
            **csrf_context(request),
        },
    )


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="terms.html",
        context={**user_context(request), "site_url": config.SITE_URL},
    )


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="privacy.html",
        context={**user_context(request), "site_url": config.SITE_URL},
    )


@app.get("/refund", response_class=HTMLResponse)
def refund(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="refund.html",
        context={**user_context(request), "site_url": config.SITE_URL},
    )


@app.get("/contact", response_class=HTMLResponse)
def contact(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="contact.html",
        context={**user_context(request), "site_url": config.SITE_URL},
    )


@app.get("/login", response_class=HTMLResponse)
def login(
    request: Request,
    joined: bool = False,
    registered: bool = False,
    verified: bool = False,
    reset: bool = False,
):
    return render_login(
        request,
        joined=joined,
        registered=registered,
        verified=verified,
        reset=reset,
    )


@app.post("/login")
def login_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
):
    if not check_csrf(request, csrf_token):
        return render_login(request, error="Your session expired. Please try again.")

    db: Session = SessionLocal()
    try:
        normalized_email = email.strip().lower()
        user = db.query(User).filter(User.email == normalized_email).first()

        if not user or not user.password_hash or not verify_password(password, user.password_hash):
            return render_login(request, error="Invalid email or password.")

        if not user.email_verified:
            return render_login(request, error="Please verify your email before logging in.")

        request.session["user_id"] = user.id
        track_event(request, "login_success", user=user)
    finally:
        db.close()

    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/auth/github/login")
def github_login(request: Request):
    if not config.github_oauth_configured:
        return render_login(request, error="GitHub login is not configured yet.")

    state = new_token()
    request.session["github_oauth_state"] = state
    params = {
        "client_id": config.GITHUB_CLIENT_ID,
        "redirect_uri": config.GITHUB_OAUTH_REDIRECT_URI,
        "scope": "read:user user:email",
        "state": state,
        "allow_signup": "true",
    }
    return RedirectResponse(
        url=f"https://github.com/login/oauth/authorize?{urlencode(params)}",
        status_code=303,
    )


@app.get("/auth/github/callback", response_class=HTMLResponse)
def github_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:
        return render_login(request, error="GitHub login was cancelled or failed.")

    expected_state = request.session.pop("github_oauth_state", None)
    if not expected_state or not state or not secrets.compare_digest(expected_state, state):
        return render_login(request, error="GitHub login session expired. Please try again.")

    if not config.github_oauth_configured:
        return render_login(request, error="GitHub login is not configured yet.")

    try:
        token_response = requests.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": config.GITHUB_CLIENT_ID,
                "client_secret": config.GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": config.GITHUB_OAUTH_REDIRECT_URI,
            },
            headers={"Accept": "application/json"},
            timeout=10,
        )
        token_response.raise_for_status()
        access_token = token_response.json().get("access_token")
        if not access_token:
            return render_login(request, error="GitHub login failed. Please try again.")

        auth_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/vnd.github+json",
        }
        profile_response = requests.get("https://api.github.com/user", headers=auth_headers, timeout=10)
        profile_response.raise_for_status()
        profile = profile_response.json()

        emails_response = requests.get("https://api.github.com/user/emails", headers=auth_headers, timeout=10)
        emails_response.raise_for_status()
        emails = emails_response.json()
    except Exception as exc:
        print(f"[/auth/github/callback error] {exc!r}")
        return render_login(request, error="GitHub login failed. Please try again.")

    verified_email = None
    for item in emails:
        if item.get("verified") and item.get("primary") and item.get("email"):
            verified_email = item["email"].strip().lower()
            break
    if not verified_email:
        for item in emails:
            if item.get("verified") and item.get("email"):
                verified_email = item["email"].strip().lower()
                break

    if not verified_email:
        return render_login(request, error="GitHub did not return a verified email address.")

    github_id = str(profile.get("id") or "")
    github_username = profile.get("login") or ""
    display_name = profile.get("name") or github_username or verified_email.split("@")[0]

    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.github_id == github_id).first() if github_id else None
        if not user:
            user = db.query(User).filter(User.email == verified_email).first()
            if user:
                user.github_id = github_id
                user.github_username = github_username
                user.email_verified = True
                if user.auth_provider == "email":
                    user.auth_provider = "email,github"
        if not user:
            user = User(
                name=display_name,
                email=verified_email,
                password_hash=None,
                plan="free",
                email_verified=True,
                github_id=github_id,
                github_username=github_username,
                auth_provider="github",
                marketing_opt_in=False,
            )
            db.add(user)

        db.commit()
        request.session["user_id"] = user.id
    finally:
        db.close()

    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/beta-signup")
def beta_signup(email: str = Form(...)):
    file_exists = BETA_FILE.exists()

    with BETA_FILE.open("a", encoding="utf-8") as f:
        if not file_exists:
            f.write("timestamp,email\n")

        f.write(f"{datetime.now(timezone.utc).isoformat()},{email}\n")

    return RedirectResponse(url="/login?joined=true", status_code=303)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return render_register(request)


@app.post("/register")
def register_user(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    marketing_opt_in: bool = Form(False),
    csrf_token: str = Form(""),
):
    if not check_csrf(request, csrf_token):
        return render_register(request, error="Your session expired. Please try again.")

    password_error = validate_password_strength(password)
    if password_error:
        return render_register(request, error=password_error)

    normalized_email = email.strip().lower()
    track_event(request, "register_submitted", metadata={"email_domain": normalized_email.split("@")[-1] if "@" in normalized_email else ""})

    db: Session = SessionLocal()
    try:
        existing_user = db.query(User).filter(User.email == normalized_email).first()

        if existing_user:
            return render_register(request, error="Email already registered. Please login instead.")

        token = new_token()
        opted_in = bool(marketing_opt_in)
        user = User(
            name=name.strip(),
            email=normalized_email,
            password_hash=hash_password(password),
            plan="free",
            email_verified=False,
            email_verification_token=token,
            email_verification_sent_at=now_utc(),
            marketing_opt_in=opted_in,
            marketing_opt_in_at=now_utc() if opted_in else None,
            auth_provider="email",
        )

        db.add(user)
        db.commit()
        email_utils.send_verification_email(normalized_email, verification_link(token))
    finally:
        db.close()

    return templates.TemplateResponse(
        request=request,
        name="verify_notice.html",
        context={"email": normalized_email, **user_context(request), **csrf_context(request)},
    )


@app.get("/verify-email", response_class=HTMLResponse)
def verify_email(request: Request, token: str = ""):
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.email_verification_token == token).first() if token else None
        if (
            not user
            or token_age_seconds(user.email_verification_sent_at) > EMAIL_VERIFICATION_MAX_AGE_SECONDS
        ):
            return templates.TemplateResponse(
                request=request,
                name="verify_email_result.html",
                context={
                    "success": False,
                    "message": "That verification link is invalid or expired.",
                    **user_context(request),
                    **csrf_context(request),
                },
                status_code=400,
            )

        user.email_verified = True
        user.email_verification_token = None
        user.email_verification_sent_at = None
        db.commit()
        track_event(request, "email_verified", user=user)
    finally:
        db.close()

    return templates.TemplateResponse(
        request=request,
        name="verify_email_result.html",
        context={
            "success": True,
            "message": "Email verified. You can now log in to BashOps Radar.",
            **user_context(request),
        },
    )


@app.post("/resend-verification", response_class=HTMLResponse)
def resend_verification(
    request: Request,
    email: str = Form(...),
    csrf_token: str = Form(""),
):
    if not check_csrf(request, csrf_token):
        return templates.TemplateResponse(
            request=request,
            name="verify_notice.html",
            context={
                "email": email,
                "message": "Your session expired. Please try again.",
                **user_context(request),
                **csrf_context(request),
            },
        )

    normalized_email = email.strip().lower()
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.email == normalized_email).first()
        if user and not user.email_verified:
            token = new_token()
            user.email_verification_token = token
            user.email_verification_sent_at = now_utc()
            db.commit()
            email_utils.send_verification_email(normalized_email, verification_link(token))
    finally:
        db.close()

    return templates.TemplateResponse(
        request=request,
        name="verify_notice.html",
        context={
            "email": normalized_email,
            "message": "If the account exists and is unverified, a new verification link has been sent.",
            **user_context(request),
            **csrf_context(request),
        },
    )


@app.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="forgot_password.html",
        context={"message": None, "error": None, **user_context(request), **csrf_context(request)},
    )


@app.post("/forgot-password", response_class=HTMLResponse)
def forgot_password(
    request: Request,
    email: str = Form(...),
    csrf_token: str = Form(""),
):
    generic_message = "If an account exists, reset instructions have been sent."
    if not check_csrf(request, csrf_token):
        return templates.TemplateResponse(
            request=request,
            name="forgot_password.html",
            context={
                "message": None,
                "error": "Your session expired. Please try again.",
                **user_context(request),
                **csrf_context(request),
            },
        )

    normalized_email = email.strip().lower()
    track_event(request, "forgot_password_requested")
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.email == normalized_email).first()
        if user and user.password_hash:
            token = new_token()
            user.password_reset_token = token
            user.password_reset_sent_at = now_utc()
            db.commit()
            email_utils.send_password_reset_email(normalized_email, reset_link(token))
    finally:
        db.close()

    return templates.TemplateResponse(
        request=request,
        name="forgot_password.html",
        context={"message": generic_message, "error": None, **user_context(request), **csrf_context(request)},
    )


@app.get("/reset-password", response_class=HTMLResponse)
def reset_password_page(request: Request, token: str = ""):
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.password_reset_token == token).first() if token else None
        valid = bool(user and token_age_seconds(user.password_reset_sent_at) <= PASSWORD_RESET_MAX_AGE_SECONDS)
    finally:
        db.close()

    return templates.TemplateResponse(
        request=request,
        name="reset_password.html",
        context={
            "token": token if valid else "",
            "valid": valid,
            "error": None if valid else "That reset link is invalid or expired.",
            **user_context(request),
            **csrf_context(request),
        },
        status_code=200 if valid else 400,
    )


@app.post("/reset-password", response_class=HTMLResponse)
def reset_password(
    request: Request,
    token: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
):
    if not check_csrf(request, csrf_token):
        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={
                "token": token,
                "valid": True,
                "error": "Your session expired. Please try again.",
                **user_context(request),
                **csrf_context(request),
            },
        )

    password_error = validate_password_strength(password)
    if password_error:
        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={
                "token": token,
                "valid": True,
                "error": password_error,
                **user_context(request),
                **csrf_context(request),
            },
        )

    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.password_reset_token == token).first() if token else None
        if (
            not user
            or token_age_seconds(user.password_reset_sent_at) > PASSWORD_RESET_MAX_AGE_SECONDS
        ):
            return templates.TemplateResponse(
                request=request,
                name="reset_password.html",
                context={
                    "token": "",
                    "valid": False,
                    "error": "That reset link is invalid or expired.",
                    **user_context(request),
                    **csrf_context(request),
                },
                status_code=400,
            )

        user.password_hash = hash_password(password)
        user.password_reset_token = None
        user.password_reset_sent_at = None
        user.email_verified = True
        db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/login?reset=true", status_code=303)


def admin_event_summary() -> dict:
    today = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    funnel_names = [
        "landing_view",
        "register_submitted",
        "analysis_completed",
        "upgrade_clicked",
        "checkout_started",
        "checkout_completed",
    ]

    def empty_summary(db: Session) -> dict:
        def safe_count(query) -> int:
            try:
                return query.count()
            except SQLAlchemyError:
                db.rollback()
                return 0

        return {
            "cards": [
                {"label": "Visitors / Events Today", "value": 0},
                {"label": "Registrations Today", "value": 0},
                {"label": "Verified Users", "value": safe_count(db.query(User).filter(User.email_verified.is_(True)))},
                {"label": "Analyses Today", "value": 0},
                {"label": "Upgrade Clicks Today", "value": 0},
                {"label": "Checkout Starts Today", "value": 0},
                {"label": "Checkout Completions Today", "value": 0},
                {"label": "Free Users", "value": safe_count(db.query(User).filter(User.plan == "free"))},
                {"label": "Pro Users", "value": safe_count(db.query(User).filter(User.plan == "pro"))},
            ],
            "recent_events": [],
            "top_events": [],
            "top_referrers": [],
            "top_repositories": [],
            "funnel": [{"name": name, "count": 0} for name in funnel_names],
        }

    db: Session = SessionLocal()
    try:
        try:
            events_today = db.query(Event).filter(Event.created_at >= today).all()
            all_events = db.query(Event).order_by(Event.created_at.desc()).limit(50).all()
        except SQLAlchemyError as e:
            db.rollback()
            print(f"[/admin/analytics event query unavailable] {e.__class__.__name__}")
            return empty_summary(db)

        def count_today(name: str) -> int:
            return sum(1 for event in events_today if event.event_name == name)

        top_event_rows = (
            db.query(Event.event_name, func.count(Event.id))
            .group_by(Event.event_name)
            .order_by(func.count(Event.id).desc())
            .limit(10)
            .all()
        )
        top_referrer_rows = (
            db.query(Event.referrer, func.count(Event.id))
            .filter(Event.referrer != "")
            .group_by(Event.referrer)
            .order_by(func.count(Event.id).desc())
            .limit(10)
            .all()
        )
        top_repo_rows = (
            db.query(Target.repo, func.count(Target.id))
            .filter(Target.repo != "")
            .group_by(Target.repo)
            .order_by(func.count(Target.id).desc())
            .limit(10)
            .all()
        )

        return {
            "cards": [
                {"label": "Visitors / Events Today", "value": len(events_today)},
                {"label": "Registrations Today", "value": count_today("register_submitted")},
                {"label": "Verified Users", "value": db.query(User).filter(User.email_verified.is_(True)).count()},
                {"label": "Analyses Today", "value": count_today("analysis_completed")},
                {"label": "Upgrade Clicks Today", "value": count_today("upgrade_clicked")},
                {"label": "Checkout Starts Today", "value": count_today("checkout_started")},
                {"label": "Checkout Completions Today", "value": count_today("checkout_completed")},
                {"label": "Free Users", "value": db.query(User).filter(User.plan == "free").count()},
                {"label": "Pro Users", "value": db.query(User).filter(User.plan == "pro").count()},
            ],
            "recent_events": [
                {
                    "event_name": event.event_name,
                    "page": event.page or "",
                    "referrer": event.referrer or "",
                    "created_at": event.created_at,
                }
                for event in all_events
            ],
            "top_events": [{"name": name, "count": count} for name, count in top_event_rows],
            "top_referrers": [{"referrer": referrer, "count": count} for referrer, count in top_referrer_rows],
            "top_repositories": [{"repo": repo, "count": count} for repo, count in top_repo_rows],
            "funnel": [{"name": name, "count": db.query(Event).filter(Event.event_name == name).count()} for name in funnel_names],
        }
    finally:
        db.close()


@app.get("/admin/analytics", response_class=HTMLResponse)
def analytics_dashboard(request: Request):
    current_user = get_current_user(request)

    # Previously this route had no auth at all and showed every user's data
    # to any visitor. It's now restricted to a single admin account
    # (set ADMIN_EMAILS in the environment) since it's a global view across
    # all accounts, not a per-user page.
    admin_response = require_admin_or_redirect(request, current_user)
    if admin_response:
        return admin_response

    summary = admin_event_summary()
    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={**summary, **user_context(request, current_user)},
    )


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request):
    current_user = get_current_user(request)
    admin_response = require_admin_or_redirect(request, current_user)
    if admin_response:
        return admin_response

    db: Session = SessionLocal()
    try:
        users = db.query(User).order_by(User.created_at.desc()).limit(50).all()
        total_users = db.query(User).count()
        verified_users = db.query(User).filter(User.email_verified.is_(True)).count()
        marketing_users = db.query(User).filter(User.marketing_opt_in.is_(True)).count()
        free_users = db.query(User).filter(User.plan == "free").count()
        pro_users = db.query(User).filter(User.plan == "pro").count()
        recent_signups = (
            db.query(User)
            .order_by(User.created_at.desc())
            .limit(5)
            .all()
        )
    finally:
        db.close()

    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context={
            "users": users,
            "recent_signups": recent_signups,
            "total_users": total_users,
            "verified_users": verified_users,
            "unverified_users": total_users - verified_users,
            "marketing_users": marketing_users,
            "free_users": free_users,
            "pro_users": pro_users,
            **user_context(request, current_user),
        },
    )


@app.get("/pipeline", response_class=HTMLResponse)
def pipeline(request: Request):
    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    summary = analytics_summary(user_id=current_user.id)
    return templates.TemplateResponse(
        request=request,
        name="pipeline.html",
        context={**summary, **user_context(request, current_user), **csrf_context(request)},
    )


@app.get("/analysis/{target_id}", response_class=HTMLResponse)
def saved_analysis(request: Request, target_id: int):
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    db: Session = SessionLocal()
    try:
        target = db.query(Target).filter(Target.id == target_id, Target.user_id == current_user.id).first()
        if not target:
            return RedirectResponse(url="/pipeline", status_code=303)

        repo_url = target.repo_url or f"https://github.com/{target.repo}"
    finally:
        db.close()

    try:
        track_event(request, "analysis_reopened", user=current_user, metadata={"target_id": target_id, "repo_url": repo_url})
        result = build_analysis_result(repo_url)
        best_issue = result.get("best_issue")
        ai_summary = generate_ai_summary(
            repo_full_name=result["repo"],
            repo=result["repo_data"],
            best_issue=best_issue,
            repo_score=result["score"],
            angle=result["angle"],
        )
        result["ai_summary"] = ai_summary["text"]
        result["ai_status"] = ai_summary["status"]

        return templates.TemplateResponse(
            request=request,
            name="analysis_result.html",
            context={
                "result": result,
                "error": None,
                "limit_reached": False,
                "from_discover": False,
                "site_url": config.SITE_URL,
                "pro_price": config.PRO_PRICE_USD,
                **user_context(request, current_user),
                **csrf_context(request),
            },
        )
    except Exception as e:
        print(f"[/analysis/{target_id} error] {e!r}")
        return templates.TemplateResponse(
            request=request,
            name="analysis_result.html",
            context={
                "result": None,
                "error": "Something went wrong reopening that analysis. Please try again.",
                "limit_reached": False,
                "from_discover": False,
                "site_url": config.SITE_URL,
                "pro_price": config.PRO_PRICE_USD,
                **user_context(request, current_user),
                **csrf_context(request),
            },
        )

SNAPSHOT_STAGE_COPY = {
    "New Target": (
        "Researching",
        "Evaluating this repository as a focused proof-of-work opportunity.",
    ),
    "Researching": (
        "Researching",
        "Evaluating this repository as a focused proof-of-work opportunity.",
    ),
    "Working": (
        "Working",
        "Actively working on a contribution for this repository.",
    ),
    "PR Submitted": (
        "PR Submitted",
        "A pull request has been submitted for review.",
    ),
    "PR Merged": (
        "PR Merged",
        "The contribution has been accepted and merged.",
    ),
    "Founder Contacted": (
        "Founder Contacted",
        "The contribution is now supporting a direct project conversation.",
    ),
    "Paid Sprint": (
        "Paid Sprint",
        "This proof-of-work journey has progressed into paid sprint work.",
    ),
    "Retainer": (
        "Retainer",
        "This contribution relationship has progressed into ongoing work.",
    ),
}


def snapshot_issue_focus(target_data: dict, result: Optional[dict] = None) -> str:
    best_issue = (result or {}).get("best_issue") or {}
    issue_type = best_issue.get("type")
    if issue_type:
        return issue_type

    stored_issue = (target_data.get("best_issue") or "").strip()
    if stored_issue and not stored_issue.startswith("#"):
        return stored_issue[:90]

    return "Focused open-source contribution"


@app.get("/pipeline/{target_id}/snapshot", response_class=HTMLResponse)
def proof_of_work_snapshot(request: Request, target_id: int):
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    db: Session = SessionLocal()
    try:
        target = db.query(Target).filter(Target.id == target_id, Target.user_id == current_user.id).first()
        if not target:
            raise HTTPException(status_code=404, detail="Snapshot not found")

        first_target = (
            db.query(Target)
            .filter(Target.user_id == current_user.id)
            .order_by(Target.created_at.asc(), Target.id.asc())
            .first()
        )
        first_target_id = first_target.id if first_target else target.id
        repo_url = target.repo_url or (f"https://github.com/{target.repo}" if target.repo else "")

        snapshot_target = {
            "id": target.id,
            "repo": target.repo or "Unknown repository",
            "repo_url": repo_url,
            "score": target.score if target.score is not None else 0,
            "status": target.status or "New Target",
            "language": target.language or "Unknown",
            "difficulty": target.difficulty or "Estimate unavailable",
            "merge_probability": target.merge_probability or "Estimate unavailable",
            "estimated_time": target.estimated_time or "Estimate unavailable",
            "stars": target.stars or 0,
            "forks": target.forks or 0,
            "open_issues": target.open_issues or 0,
            "best_issue": target.best_issue or "",
        }
    finally:
        db.close()

    has_access = has_pro_access(current_user)
    stage_label, stage_copy = SNAPSHOT_STAGE_COPY.get(
        snapshot_target["status"],
        ("Researching", "Evaluating this repository as a focused proof-of-work opportunity."),
    )
    free_stage_allowed = stage_label == "Researching"
    free_target_allowed = target_id == first_target_id
    locked = not has_access and (not free_target_allowed or not free_stage_allowed)

    if locked:
        return templates.TemplateResponse(
            request=request,
            name="proof_of_work_snapshot.html",
            context={
                "locked": True,
                "stage_label": stage_label,
                "stage_copy": stage_copy,
                "target": None,
                "signals": [],
                "focus": "",
                "site_url": config.SITE_URL,
                **user_context(request, current_user),
            },
        )

    signals = []
    focus = snapshot_issue_focus(snapshot_target)
    if snapshot_target["repo_url"]:
        try:
            result = build_analysis_result(snapshot_target["repo_url"])
            signals = (result.get("score_transparency") or {}).get("reasons", [])[:5]
            focus = snapshot_issue_focus(snapshot_target, result)
        except Exception as e:
            print(f"[/pipeline/{target_id}/snapshot analysis enrich error] {e!r}")

    track_event(
        request,
        "snapshot_viewed",
        user=current_user,
        metadata={
            "target_id": target_id,
            "repo": snapshot_target["repo"],
            "plan": "pro" if has_access else "free",
            "stage": stage_label,
        },
    )

    return templates.TemplateResponse(
        request=request,
        name="proof_of_work_snapshot.html",
        context={
            "locked": False,
            "stage_label": stage_label,
            "stage_copy": stage_copy,
            "target": snapshot_target,
            "signals": signals,
            "focus": focus,
            "site_url": config.SITE_URL,
            **user_context(request, current_user),
        },
    )

@app.post("/analyze", response_class=HTMLResponse)
def analyze(
    request: Request,
    repo_url: str = Form(...),
    csrf_token: str = Form(""),
    source: str = Form(""),
):
    current_user = get_current_user(request)
    ip = request.client.host if request.client else "unknown"

    try:
        if not check_csrf(request, csrf_token):
            return templates.TemplateResponse(
                request=request,
                name="analysis_result.html",
                context={
                    "result": None,
                    "error": "Your session expired. Please try again.",
                    "limit_reached": False,
                    "from_discover": source == "discover",
                    "site_url": config.SITE_URL,
                    "pro_price": config.PRO_PRICE_USD,
                    **user_context(request, current_user),
                    **csrf_context(request),
                },
            )

        if source == "discover":
            track_event(
                request,
                "discover_result_clicked",
                user=current_user,
                metadata={"repo_url": repo_url},
            )

        if current_user and not has_pro_access(current_user) and not current_user.email_verified:
            track_event(
                request,
                "free_analysis_blocked",
                user=current_user,
                metadata={"reason": "email_unverified"},
            )
            return templates.TemplateResponse(
                request=request,
                name="analysis_result.html",
                context={
                    "result": None,
                    "error": None,
                    "limit_reached": False,
                    "verification_required": True,
                    "from_discover": source == "discover",
                    "site_url": config.SITE_URL,
                    "pro_price": config.PRO_PRICE_USD,
                    **user_context(request, current_user),
                    **csrf_context(request),
                },
            )

        if current_user:
            count = free_account_analysis_count(current_user)
            over_limit = not has_pro_access(current_user) and count >= FREE_ANALYSIS_LIMIT
            limit_type = "account"
        else:
            over_limit = anonymous_website_analysis_used(request, ip)
            limit_type = "anonymous"

        if over_limit:
            track_event(
                request,
                "free_analysis_blocked",
                user=current_user,
                metadata={"reason": limit_type, "repo_url": repo_url},
            )
            return templates.TemplateResponse(
                request=request,
                name="analysis_result.html",
                context={
                    "result": None,
                    "error": None,
                    "limit_reached": True,
                    "limit_type": limit_type,
                    "from_discover": source == "discover",
                    "site_url": config.SITE_URL,
                    "pro_price": config.PRO_PRICE_USD,
                    **user_context(request, current_user),
                    **csrf_context(request),
                },
            )

        track_event(request, "analysis_started", user=current_user, metadata={"repo_url": repo_url})
        result = build_analysis_result(repo_url)
        best_issue = result.get("best_issue")

        ai_summary = generate_ai_summary(
            repo_full_name=result["repo"],
            repo=result["repo_data"],
            best_issue=best_issue,
            repo_score=result["score"],
            angle=result["angle"],
        )

        result["ai_summary"] = ai_summary["text"]
        result["ai_status"] = ai_summary["status"]

        track_analysis(
            repo=result["repo"],
            repo_url=result["repo_url"],
            score=result["score"],
            best_issue=f"#{best_issue['number']}" if best_issue else "",
            best_issue_url=best_issue["url"] if best_issue else "",
            request=request,
            user_id=current_user.id if current_user else None,
            language=result.get("language", "Unknown"),
            stars=result.get("stars", 0),
            forks=result.get("forks", 0),
            open_issues=result.get("open_issues", 0),
            merge_probability=result.get("merge_probability"),
            difficulty=result.get("difficulty"),
            estimated_time=result.get("estimated_time"),
        )

        if current_user and not has_pro_access(current_user):
            used_count = free_account_analysis_count(current_user)
            remaining_count = max(FREE_ANALYSIS_LIMIT - used_count, 0)
            track_event(
                request,
                "free_analysis_completed",
                user=current_user,
                metadata={"repo": result["repo"], "remaining": remaining_count},
            )
            if remaining_count == 0:
                track_event(
                    request,
                    "free_trial_completed",
                    user=current_user,
                    metadata={"repo": result["repo"]},
                )
        elif not current_user:
            request.session["anonymous_analysis_used"] = True
            track_event(
                request,
                "free_analysis_completed",
                metadata={"repo": result["repo"], "anonymous": True},
            )

        track_event(
            request,
            "analysis_completed",
            user=current_user,
            metadata={"repo": result["repo"], "score": result["score"]},
        )

        return templates.TemplateResponse(
            request=request,
            name="analysis_result.html",
            context={
                "result": result,
                "error": None,
                "limit_reached": False,
                "from_discover": source == "discover",
                "site_url": config.SITE_URL,
                "pro_price": config.PRO_PRICE_USD,
                **user_context(request, current_user),
                **csrf_context(request),
            },
        )

    except Exception as e:
        # Only a safe, generic message reaches the user now — raw
        # exception text (which can include GitHub API response bodies)
        # is logged server-side instead of rendered into the page.
        print(f"[/analyze error] {e!r}")

        error_text = str(e)
        safe_prefixes = ("Please provide a valid", "GitHub API rate limit", "GitHub API timed out", "Could not connect to GitHub API")
        message = error_text if error_text.startswith(safe_prefixes) else (
            "Something went wrong analyzing that repository. Please check the URL and try again."
        )

        return templates.TemplateResponse(
            request=request,
            name="analysis_result.html",
            context={
                "result": None,
                "error": message,
                "limit_reached": False,
                "from_discover": source == "discover",
                "site_url": config.SITE_URL,
                "pro_price": config.PRO_PRICE_USD,
                **user_context(request, current_user),
                **csrf_context(request),
            },
        )


@app.post("/api/v1/analyze")
async def api_analyze(request: Request):
    current_user = get_current_user(request)
    ip = request.client.host if request.client else "unknown"
    site_url = config.SITE_URL.rstrip("/")

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON payload."}, status_code=400)

    repo_url = (payload.get("repo_url") or "").strip()
    issue_number = payload.get("issue_number")
    if not repo_url:
        return JSONResponse({"error": "repo_url is required."}, status_code=400)

    if current_user:
        count = daily_analysis_count(user_id=current_user.id)
        over_limit = not has_pro_access(current_user) and count >= FREE_ANALYSIS_LIMIT
    else:
        count = daily_analysis_count(ip=ip)
        over_limit = count >= FREE_ANALYSIS_LIMIT

    if over_limit:
        return JSONResponse(
            {
                "error": "Free analysis limit reached.",
                "upgrade_url": f"{site_url}/pricing",
            },
            status_code=429,
        )

    client_name = request.headers.get("X-BashOps-Client", "").strip().lower()
    event_prefix = "github_action" if client_name == "github-action" else "api"
    track_event(
        request,
        f"{event_prefix}_analysis_started",
        user=current_user,
        metadata={"repo_url": repo_url},
    )

    try:
        result = build_analysis_result(repo_url, issue_number=issue_number)
        response_payload = to_public_api_payload(result, site_url)

        try:
            best_issue = result.get("best_issue")
            track_analysis(
                repo=result["repo"],
                repo_url=result["repo_url"],
                score=result["score"],
                best_issue=f"#{best_issue['number']}" if best_issue else "",
                best_issue_url=best_issue["url"] if best_issue else "",
                request=request,
                user_id=current_user.id if current_user else None,
                language=result.get("language", "Unknown"),
                stars=result.get("stars", 0),
                forks=result.get("forks", 0),
                open_issues=result.get("open_issues", 0),
                merge_probability=result.get("merge_probability"),
                difficulty=result.get("difficulty"),
                estimated_time=result.get("estimated_time"),
            )
        except Exception as e:
            print(f"[/api/v1/analyze tracking error] {e!r}")

        track_event(
            request,
            f"{event_prefix}_analysis_completed",
            user=current_user,
            metadata={"repo": result["repo"], "score": result["score"]},
        )
        return response_payload
    except Exception as e:
        print(f"[/api/v1/analyze error] {e!r}")
        error_text = str(e)
        safe_prefixes = (
            "Please provide a valid",
            "GitHub API rate limit",
            "GitHub API timed out",
            "Could not connect to GitHub API",
        )
        message = (
            error_text
            if error_text.startswith(safe_prefixes)
            else "Something went wrong analyzing that repository. Please check the URL and try again."
        )
        status_code = 429 if "rate limit" in error_text.lower() else 400
        return JSONResponse({"error": message}, status_code=status_code)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    summary = analytics_summary(user_id=current_user.id)
    analyses_used_total = free_account_analysis_count(current_user)
    analyses_remaining = None
    if not has_pro_access(current_user):
        analyses_remaining = max(FREE_ANALYSIS_LIMIT - analyses_used_total, 0)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            **summary,
            "free_analysis_limit": FREE_ANALYSIS_LIMIT,
            "analyses_used_total": analyses_used_total,
            "analyses_remaining": analyses_remaining,
            **user_context(request, current_user),
            **csrf_context(request),
        },
    )


@app.post("/update-status")
def update_status(
    request: Request,
    repo: str = Form(...),
    status: str = Form(...),
    csrf_token: str = Form(""),
):
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    if check_csrf(request, csrf_token):
        update_pipeline_status(repo, status, user_id=current_user.id)

    return RedirectResponse(url="/pipeline", status_code=303)


@app.post("/generate-pitch", response_class=HTMLResponse)
def pitch_preview(
    request: Request,
    repo: str = Form(...),
    best_issue: str = Form(""),
    csrf_token: str = Form(""),
):
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    summary = analytics_summary(user_id=current_user.id)

    if not check_csrf(request, csrf_token):
        return templates.TemplateResponse(
            request=request,
            name="pipeline.html",
            context={**summary, **user_context(request, current_user), **csrf_context(request)},
        )

    if not has_pro_access(current_user):
        return templates.TemplateResponse(
            request=request,
            name="pipeline.html",
            context={
                **summary,
                **user_context(request, current_user),
                "pitch_upgrade_message": "Founder pitch generation is a Pro feature. Upgrade to prepare focused outreach after your first useful pull request.",
                **csrf_context(request),
            },
        )

    pitch = generate_pitch(repo, best_issue)
    save_pitch(repo, current_user.id, pitch)

    summary = analytics_summary(user_id=current_user.id)

    return templates.TemplateResponse(
        request=request,
        name="pipeline.html",
        context={
            **summary,
            **user_context(request, current_user),
            "generated_pitch": pitch,
            "pitch_repo": repo,
            "pitch_is_pro": True,
            **csrf_context(request),
        },
    )


# --- Billing (Paddle) ------------------------------------------------------
@app.get("/billing/upgrade")
def billing_upgrade(request: Request):
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/register", status_code=303)

    track_event(request, "upgrade_clicked", user=current_user)

    if current_user.plan == "pro":
        return RedirectResponse(url="/dashboard", status_code=303)

    if not config.paddle_configured:
        return templates.TemplateResponse(
            request=request,
            name="pricing.html",
            context={
                "billing_error": "Billing is not configured yet. Set Paddle environment variables before accepting payments.",
                "pro_price": config.PRO_PRICE_USD,
                "site_url": config.SITE_URL,
                **user_context(request, current_user),
            },
        )

    track_event(request, "checkout_started", user=current_user)
    return templates.TemplateResponse(
        request=request,
        name="checkout.html",
        context={
            "paddle_client_token": config.PADDLE_CLIENT_TOKEN,
            "paddle_price_id": config.PADDLE_PRICE_ID,
            "paddle_env": config.PADDLE_ENV,
            "user_email": current_user.email,
            "user_id": current_user.id,
            "site_url": config.SITE_URL,
            **user_context(request, current_user),
        },
    )


@app.get("/billing/success")
def billing_success(request: Request):
    # The plan upgrade itself happens from the verified webhook, not this
    # redirect — a user landing here without paying stays on whatever plan
    # they already had. This page is just a friendly landing spot.
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="billing_success.html",
        context={**user_context(request, current_user), "site_url": config.SITE_URL},
    )


@app.get("/billing/manage", response_class=HTMLResponse)
def billing_manage(request: Request):
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="billing_manage.html",
        context={**user_context(request, current_user), "site_url": config.SITE_URL},
    )


@app.post("/billing/webhook")
async def paddle_webhook(request: Request):
    payload = await request.body()

    try:
        event = paddle_billing.verify_paddle_webhook(payload, request.headers)
    except paddle_billing.PaddleBillingNotConfigured as e:
        print(f"[/billing/webhook not configured] {e!r}")
        return JSONResponse({"error": "billing not configured"}, status_code=500)
    except paddle_billing.PaddleSignatureError as e:
        print(f"[/billing/webhook signature error] {e!r}")
        return JSONResponse({"error": "invalid signature"}, status_code=400)
    except Exception as e:
        print(f"[/billing/webhook parse error] {e!r}")
        return JSONResponse({"error": "invalid payload"}, status_code=400)

    try:
        paddle_billing.handle_paddle_webhook_event(event)
    except Exception as e:
        print(f"[/billing/webhook handler error] {e!r}")
        return JSONResponse({"error": "webhook handling failed"}, status_code=500)

    if event.get("event_type") in {"transaction.completed", "subscription.created", "subscription.updated"}:
        track_event(
            request,
            "checkout_completed",
            metadata={
                "event_type": event.get("event_type"),
                "customer_id": (event.get("data") or {}).get("customer_id"),
                "subscription_id": (event.get("data") or {}).get("subscription_id") or (event.get("data") or {}).get("id"),
            },
        )

    return {"received": True}
