import os
from analytics import analytics_summary, track_analysis, update_pipeline_status, generate_pitch
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from analytics import analytics_summary, track_analysis
from radar import get_analysis, decision, recommend_angle

try:
    import google.generativeai as genai
except Exception:
    genai = None


app = FastAPI(title="BashOps Radar")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY and genai:
    genai.configure(api_key=GEMINI_API_KEY)


def generate_ai_summary(repo_full_name, repo, best_issue, repo_score, angle):
    if not GEMINI_API_KEY or not genai:
        return "AI summary is not enabled yet. Add GEMINI_API_KEY to enable Gemini analysis."

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
        return response.text or "AI summary could not be generated."
    except Exception as e:
        return f"AI summary failed: {e}"


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"result": None, "error": None},
    )


@app.get("/admin/analytics", response_class=HTMLResponse)
def analytics_dashboard(request: Request):
    summary = analytics_summary()

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={
            "total_analyses": summary["total_analyses"],
            "unique_repos": summary["unique_repos"],
            "average_score": summary["average_score"],
            "highest_score": summary["highest_score"],
            "top_repos": summary["top_repos"],
            "top_issues": summary["top_issues"],
            "rows": summary["rows"][-20:],
        },
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
        owner, repo_name, repo, languages, issue_rankings, repo_score = get_analysis(repo_url)

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
            "website": repo.get("homepage") or "Not found",
            "github": repo.get("html_url"),
            "description": repo.get("description"),
            "stars": repo.get("stargazers_count"),
            "forks": repo.get("forks_count"),
            "open_issues": repo.get("open_issues_count"),
            "last_push": repo.get("pushed_at"),
            "score": repo_score,
            "score_label": "Excellent"
            if repo_score >= 90
            else "Strong"
            if repo_score >= 80
            else "Moderate"
            if repo_score >= 60
            else "Weak",
            "score_action": "CONTRIBUTE NOW"
            if repo_score >= 85
            else "INSPECT MANUALLY"
            if repo_score >= 60
            else "SKIP FOR NOW",
            "merge_probability": "High" if repo_score >= 85 else "Medium" if repo_score >= 60 else "Low",
            "estimated_time": "2-4 hours" if repo_score >= 85 else "4-8 hours",
            "difficulty": "Medium",
            "decision": decision(repo_score),
            "angle": angle,
            "ai_summary": ai_summary,
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
        )

        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"result": result, "error": None},
        )

    except Exception as e:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"result": None, "error": str(e)},
        )
