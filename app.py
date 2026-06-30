import os
import csv
import os
from database import Base
from database import engine
from sqlalchemy import text

from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from database import SessionLocal
from models import User
from auth import verify_password

import models
from pathlib import Path
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from database import SessionLocal
from models import User
from auth import hash_password, verify_password

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from analytics import (
    analytics_summary,
    track_analysis,
    update_pipeline_status,
    generate_pitch,
)
from radar import get_analysis, decision, recommend_angle

try:
    import google.generativeai as genai
except Exception:
    genai = None


app = FastAPI(title="BashOps Radar")
Base.metadata.create_all(bind=engine)
with engine.begin() as connection:
    connection.execute(
        text("ALTER TABLE targets ADD COLUMN IF NOT EXISTS user_id INTEGER")
    )
SECRET_KEY = os.getenv("SECRET_KEY")

if not SECRET_KEY:     
    raise RuntimeError(
        "SECRET_KEY environment variable is required."
    )

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


def daily_analysis_count(request: Request):
    analytics_file = Path("analytics.csv")

    if not analytics_file.exists():
        return 0

    ip = request.client.host if request.client else "unknown"
    today = datetime.now(timezone.utc).date().isoformat()
    count = 0

    with analytics_file.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)

        for row in reader:
            timestamp = row.get("timestamp", "")
            row_ip = row.get("ip", "")

            if timestamp.startswith(today) and row_ip == ip:
                count += 1

    return count
def get_current_user(request: Request):
    user_id = request.session.get("user_id")

    if not user_id:
        return None

    db: Session = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    db.close()

    return user

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "result": None,
            "error": None,
            "limit_reached": False,
            "current_user": get_current_user(request),
        },
    )
@app.get("/export-pipeline")
def export_pipeline():

    file = Path("analytics.csv")

    if not file.exists():

        return RedirectResponse("/pipeline")

    return FileResponse(

        path=file,

        filename="bashops_pipeline.csv",

        media_type="text/csv"

    )

@app.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="pricing.html",
        context={"result": None, "error": None, "limit_reached": False},
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
            "current_user": get_current_user(request),
        },
    )
@app.post("/login")
def login_user(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    db: Session = SessionLocal()
    user = db.query(User).filter(User.email == email).first()

    if not user or not verify_password(password, user.password_hash):
        db.close()
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "joined": False,
                "registered": False,
                "error": "Invalid email or password.",
                "current_user": None,
            },
        )

    request.session["user_id"] = user.id
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
        context={"error": None},
    )


@app.post("/register")
def register_user(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
):
    db: Session = SessionLocal()

    existing_user = db.query(User).filter(User.email == email).first()

    if existing_user:
        db.close()
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={"error": "Email already registered. Please login instead."},
        )

    user = User(
        name=name,
        email=email,
        password_hash=hash_password(password),
        plan="free",
    )

    db.add(user)
    db.commit()
    db.close()

    return RedirectResponse(url="/login?registered=true", status_code=303)


@app.get("/admin/analytics", response_class=HTMLResponse)
def analytics_dashboard(request: Request):
    summary = analytics_summary()

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context=summary,
    )


@app.get("/pipeline", response_class=HTMLResponse)
def pipeline(request: Request):
    summary = analytics_summary()

    return templates.TemplateResponse(
        request=request,
        name="pipeline.html",
        context=summary,
    )


@app.post("/analyze", response_class=HTMLResponse)
def analyze(request: Request, repo_url: str = Form(...)):
    try:
        current_user = get_current_user(request)

        if not current_user:
            return RedirectResponse(url="/login", status_code=303)

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
            score=result["score"],
            best_issue=f"#{best_issue['number']}" if best_issue else "",
            request=request,
            language=result.get("language", "Unknown"),
            stars=result.get("stars", ""),
            forks=result.get("forks", ""),
            open_issues=result.get("open_issues", ""),
            user_id=current_user.id,
        )

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"result": result, "error": None, "limit_reached": False},
        )

    except Exception as e:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"result": None, "error": str(e), "limit_reached": False},
        )
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    current_user = get_current_user(request)

    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    summary = analytics_summary()
    summary["current_user"] = current_user

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=summary,
    )
@app.post("/update-status")
def update_status(repo: str = Form(...), status: str = Form(...)):
    update_pipeline_status(repo, status)
    return RedirectResponse(url="/pipeline", status_code=303)


@app.post("/generate-pitch", response_class=HTMLResponse)
def pitch_preview(request: Request, repo: str = Form(...), best_issue: str = Form("")):
    pitch = generate_pitch(repo, best_issue)
    summary = analytics_summary()

    return templates.TemplateResponse(
        request=request,
        name="pipeline.html",
        context={
            **summary,
            "generated_pitch": pitch,
            "pitch_repo": repo,
        },
    )
