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
            "decision": decision(repo_score),
            "angle": recommend_angle(languages),
            "best_issue": best_issue,
            "issues": issue_rankings[:8],
            "languages": languages,
        }

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