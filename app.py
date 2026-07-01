import os
from pathlib import Path
from datetime import datetime, timezone

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session

from database import SessionLocal
from models import User
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
import billing
from stripe import SignatureVerificationError, StripeError
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
    urls = ["/", "/pricing", "/login", "/register"]
    body = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for path in urls:
        body.append(f"<url><loc>{config.SITE_URL}{path}</loc></url>")
    body.append("</urlset>")
    return Response(content="\n".join(body), media_type="application/xml")


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "result": None,
            "error": None,
            "limit_reached": False,
            "site_url": config.SITE_URL,
            "pro_price": config.PRO_PRICE_USD,
            **user_context(request),
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
            **user_context(request),
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login(request: Request, joined: bool = False, registered: bool = False):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "joined": joined,
            "registered": registered,
            "error": None,
            **user_context(request),
            **csrf_context(request),
        },
    )


@app.post("/login")
def login_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
):
    if not check_csrf(request, csrf_token):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "joined": False,
                "registered": False,
                "error": "Your session expired. Please try again.",
                "current_user": None,
                "is_admin": False,
                **csrf_context(request),
            },
        )

    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()

        if not user or not verify_password(password, user.password_hash):
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "joined": False,
                    "registered": False,
                    "error": "Invalid email or password.",
                    "current_user": None,
                    "is_admin": False,
                    **csrf_context(request),
                },
            )

        request.session["user_id"] = user.id
    finally:
        db.close()

    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


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
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"error": None, **csrf_context(request)},
    )


@app.post("/register")
def register_user(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(""),
):
    if not check_csrf(request, csrf_token):
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"error": "Your session expired. Please try again.", **csrf_context(request)},
        )

    db: Session = SessionLocal()
    try:
        existing_user = db.query(User).filter(User.email == email).first()

        if existing_user:
            return templates.TemplateResponse(
                request=request,
                name="register.html",
                context={"error": "Email already registered. Please login instead.", **csrf_context(request)},
            )

        user = User(
            name=name,
            email=email,
            password_hash=hash_password(password),
            plan="free",
        )

        db.add(user)
        db.commit()
    finally:
        db.close()

    return RedirectResponse(url="/login?registered=true", status_code=303)


@app.get("/admin/analytics", response_class=HTMLResponse)
def analytics_dashboard(request: Request):
    current_user = get_current_user(request)

    # Previously this route had no auth at all and showed every user's data
    # to any visitor. It's now restricted to a single admin account
    # (set ADMIN_EMAIL in the environment) since it's a global view across
    # all accounts, not a per-user page.
    if not is_admin(current_user):
        return RedirectResponse(url="/login", status_code=303)

    summary = analytics_summary(user_id=None)
    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={**summary, **user_context(request, current_user), **csrf_context(request)},
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
                name="index.html",
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
                name="index.html",
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

        return templates.TemplateResponse(
            request=request,
            name="index.html",
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
            name="index.html",
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

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={**summary, **user_context(request, current_user), **csrf_context(request)},
    )

@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="terms.html",
        context={**user_context(request)},
    )


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="privacy.html",
        context={**user_context(request)},
    )


@app.get("/refund", response_class=HTMLResponse)
def refund_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="refund.html",
        context={**user_context(request)},
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


# --- Billing (Stripe) ------------------------------------------------------
# See billing.py for the full Dashboard setup steps required before these
# routes work. Until STRIPE_SECRET_KEY / STRIPE_PRO_PRICE_ID are set, the
# upgrade button shows a clear "billing not configured" message instead of
# a broken redirect.

@app.get("/billing/upgrade")
def billing_upgrade(request: Request):
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/register", status_code=303)

    if current_user.plan == "pro":
        return RedirectResponse(url="/dashboard", status_code=303)

    base_url = str(request.base_url).rstrip("/")

    try:
        checkout_url = billing.create_checkout_session(
            user=current_user,
            success_url=f"{base_url}/billing/success",
            cancel_url=f"{base_url}/pricing",
        )
    except billing.BillingNotConfigured as e:
        return templates.TemplateResponse(
            request=request,
            name="pricing.html",
            context={
                "billing_error": str(e),
                "pro_price": config.PRO_PRICE_USD,
                **user_context(request, current_user),
            },
        )
    except StripeError as e:
        print(f"[/billing/upgrade error] {e!r}")
        return templates.TemplateResponse(
            request=request,
            name="pricing.html",
            context={
                "billing_error": "Something went wrong starting checkout. Please try again shortly.",
                "pro_price": config.PRO_PRICE_USD,
                **user_context(request, current_user),
            },
        )

    return RedirectResponse(url=checkout_url, status_code=303)


@app.get("/billing/success")
def billing_success(request: Request):
    # The plan upgrade itself happens from the verified webhook, not this
    # redirect — a user landing here without paying stays on whatever plan
    # they already had. This page is just a friendly landing spot.
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    return RedirectResponse(url="/dashboard?upgraded=true", status_code=303)


@app.get("/billing/portal")
def billing_portal_redirect(request: Request):
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    base_url = str(request.base_url).rstrip("/")

    try:
        portal_url = billing.create_portal_session(
            user=current_user,
            return_url=f"{base_url}/dashboard",
        )
    except billing.BillingNotConfigured:
        return RedirectResponse(url="/pricing", status_code=303)
    except StripeError as e:
        print(f"[/billing/portal error] {e!r}")
        return RedirectResponse(url="/dashboard", status_code=303)

    return RedirectResponse(url=portal_url, status_code=303)


@app.post("/billing/webhook")
async def billing_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = billing.construct_webhook_event(payload, sig_header)
    except billing.BillingNotConfigured as e:
        print(f"[/billing/webhook not configured] {e!r}")
        return JSONResponse({"error": "billing not configured"}, status_code=500)
    except SignatureVerificationError:
        # Wrong/missing signature — reject without processing. This is the
        # only thing standing between this endpoint and anyone on the
        # internet POSTing a fake "checkout completed" event to grant
        # themselves Pro for free, so it fails closed.
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    try:
        billing.handle_webhook_event(event)
    except Exception as e:
        # Stripe retries webhook deliveries on non-2xx responses, so a
        # transient DB error here should surface as a failure (5xx) rather
        # than being silently swallowed — otherwise a user could pay and
        # never get upgraded with no record of why.
        print(f"[/billing/webhook handler error] {e!r}")
        return JSONResponse({"error": "webhook handling failed"}, status_code=500)

    return {"received": True}
