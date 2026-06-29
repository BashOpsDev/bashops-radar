from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from radar import get_analysis, decision, recommend_angle

app = FastAPI(title="BashOps Radar")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"result": None, "error": None},
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
            "score_label": "Excellent" if repo_score >= 90 else "Strong" if repo_score >= 80 else "Moderate" if repo_score >= 60 else "Weak",
            "score_action": "CONTRIBUTE NOW" if repo_score >= 85 else "INSPECT MANUALLY" if repo_score >= 60 else "SKIP FOR NOW",
            "merge_probability": "High" if repo_score >= 85 else "Medium" if repo_score >= 60 else "Low",
            "estimated_time": "2-4 hours" if repo_score >= 85 else "4-8 hours",
            "difficulty": "Medium",
            "decision": decision(repo_score),
            "angle": recommend_angle(languages),
            "best_issue": best_issue,
            "issues": issue_rankings[:8],
            "languages": languages,
        }
"recommended_action": f"Start with {best_issue['number']} — {best_issue['title']}" if best_issue else "Analyze another repository",
"recommended_outcome": "Submit one focused PR, build trust, then pitch a 48-hour sprint.",

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