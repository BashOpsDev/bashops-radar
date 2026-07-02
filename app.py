import os
import json
import secrets
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import func
from sqlalchemy.orm import Session
import requests

from database import SessionLocal
from models import Event, Target, User
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
    fallback_pitch,
    save_pitch,
    daily_analysis_count,
)
import paddle_billing
import email_utils
from radar import get_analysis, decision, recommend_angle
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
FREE_ANALYSIS_LIMIT = 5
EMAIL_VERIFICATION_MAX_AGE_SECONDS = 24 * 60 * 60
PASSWORD_RESET_MAX_AGE_SECONDS = 60 * 60

if GEMINI_API_KEY and genai:
    genai.configure(api_key=GEMINI_API_KEY)


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

Write a concise analysis with:
1. Why this repo is worth or not worth contributing to
2. Why the best issue is a good first target
3. How the developer should approach the PR
4. Whether this could lead to a paid sprint

Keep it practical, direct, and under 180 words.
"""

    try:
        model = genai.GenerativeModel("gemini-2.5-flash-lite")
        response = model.generate_content(prompt)

        return {
            "text": response.text.strip() if response.text else "AI summary temporarily unavailable.",
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
    return bool(user and user.email and user.email.lower() in config.ADMIN_EMAILS)


def user_context(request: Request, current_user=None) -> dict:
    """Standard current_user / is_admin pair every template needs for the
    navbar to render the right links."""
    if current_user is None:
        current_user = get_current_user(request)
    return {"current_user": current_user, "is_admin": is_admin(current_user)}


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
        "Disallow: /export-pipeline",
        f"Sitemap: {config.SITE_URL}/sitemap.xml",
    ]
    return Response(content="\n".join(lines) + "\n", media_type="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    # Only the public, indexable marketing pages — the app pages behind
    # login are excluded via robots.txt above and noindex tags on the
    # pages themselves.
    urls = ["/", "/pricing", "/login", "/register", "/terms", "/privacy", "/refund", "/contact"]
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

    db: Session = SessionLocal()
    try:
        events_today = db.query(Event).filter(Event.created_at >= today).all()
        all_events = db.query(Event).order_by(Event.created_at.desc()).limit(50).all()

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
                {"label": "Events Today", "value": len(events_today)},
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
    # (set ADMIN_EMAIL in the environment) since it's a global view across
    # all accounts, not a per-user page.
    if not is_admin(current_user):
        return RedirectResponse(url="/login", status_code=303)

    summary = admin_event_summary()
    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={**summary, **user_context(request, current_user)},
    )


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request):
    current_user = get_current_user(request)
    if not is_admin(current_user):
        return RedirectResponse(url="/login", status_code=303)

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


@app.post("/analyze", response_class=HTMLResponse)
def analyze(request: Request, repo_url: str = Form(...), csrf_token: str = Form("")):
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
                    "site_url": config.SITE_URL,
                    "pro_price": config.PRO_PRICE_USD,
                    **user_context(request, current_user),
                    **csrf_context(request),
                },
            )

        if current_user:
            count = daily_analysis_count(user_id=current_user.id)
            over_limit = current_user.plan == "free" and count >= FREE_ANALYSIS_LIMIT
        else:
            count = daily_analysis_count(ip=ip)
            over_limit = count >= FREE_ANALYSIS_LIMIT

        if over_limit:
            return templates.TemplateResponse(
                request=request,
                name="analysis_result.html",
                context={
                    "result": None,
                    "error": None,
                    "limit_reached": True,
                    "site_url": config.SITE_URL,
                    "pro_price": config.PRO_PRICE_USD,
                    **user_context(request, current_user),
                    **csrf_context(request),
                },
            )

        track_event(request, "analysis_started", user=current_user, metadata={"repo_url": repo_url})
        owner, repo_name, repo, languages, issue_rankings, repo_score, language_badge = get_analysis(repo_url)

        best_issue = None
        if issue_rankings:
            score, issue_type, issue = issue_rankings[0]
            best_issue = {
                "number": issue.get("number"),
                "title": issue.get("title"),
                "url": issue.get("html_url"),
                "score": score,
                "type": issue_type,
            }

        recommended_action = "Analyze another repository"
        if best_issue:
            recommended_action = f"Start with #{best_issue['number']} - {best_issue['title']}"

        angle = recommend_angle(languages)

        ai_summary = generate_ai_summary(
            repo_full_name=f"{owner}/{repo_name}",
            repo=repo,
            best_issue=best_issue,
            repo_score=repo_score,
            angle=angle,
        )

        result = {
            "repo": f"{owner}/{repo_name}",
            "repo_url": repo_url,
            "language": language_badge,
            "website": repo.get("homepage") or "Not found",
            "github": repo.get("html_url"),
            "description": repo.get("description"),
            "stars": repo.get("stargazers_count"),
            "forks": repo.get("forks_count"),
            "open_issues": repo.get("open_issues_count"),
            "last_push": repo.get("pushed_at"),
            "score": repo_score,
            "score_label": (
                "Excellent"
                if repo_score >= 90
                else "Strong"
                if repo_score >= 80
                else "Moderate"
                if repo_score >= 60
                else "Weak"
            ),
            "score_action": (
                "CONTRIBUTE NOW"
                if repo_score >= 85
                else "INSPECT MANUALLY"
                if repo_score >= 60
                else "SKIP FOR NOW"
            ),
            "merge_probability": (
                "High"
                if repo_score >= 85
                else "Medium"
                if repo_score >= 60
                else "Low"
            ),
            "estimated_time": "2-4 hours" if repo_score >= 85 else "4-8 hours",
            "difficulty": "Medium",
            "decision": decision(repo_score),
            "angle": angle,
            "ai_summary": ai_summary["text"],
            "ai_status": ai_summary["status"],
            "best_issue": best_issue,
            "recommended_action": recommended_action,
            "recommended_outcome": "Submit one focused PR, build trust, then pitch a 48-hour sprint.",
            "issues": issue_rankings[:8],
            "languages": languages,
        }

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
                "site_url": config.SITE_URL,
                "pro_price": config.PRO_PRICE_USD,
                **user_context(request, current_user),
                **csrf_context(request),
            },
        )


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    summary = analytics_summary(user_id=current_user.id)
    analyses_used_today = daily_analysis_count(user_id=current_user.id)
    analyses_remaining = None
    if current_user.plan == "free":
        analyses_remaining = max(FREE_ANALYSIS_LIMIT - analyses_used_today, 0)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            **summary,
            "free_analysis_limit": FREE_ANALYSIS_LIMIT,
            "analyses_used_today": analyses_used_today,
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

    is_pro = current_user.plan == "pro"
    pitch = generate_pitch(repo, best_issue) if is_pro else fallback_pitch(repo, best_issue)

    # Only persist (and only "spend" a Gemini call on) pitches for Pro
    # users. A free user re-clicking the button just regenerates the same
    # free template — cheap, and doesn't need to be saved since it's
    # already what /pipeline shows by default for that row.
    if is_pro:
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
            "pitch_is_pro": is_pro,
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
