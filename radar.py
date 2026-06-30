import csv
import sys
import os
import requests
from pathlib import Path
from datetime import datetime, timezone

import requests
from rich.console import Console
from rich.table import Table
from config import APP_NAME, APP_VERSION, PUBLIC_MODE

console = Console()
TARGETS_FILE = Path("targets.csv")


def parse_github_url(url: str):
    parts = url.rstrip("/").split("/")
    if "github.com" not in url or len(parts) < 5:
        raise ValueError("Please provide a valid GitHub repo URL.")
    return parts[-2], parts[-1]


def github_get(endpoint: str):
    try:
        response = requests.get(
    f"https://api.github.com{endpoint}",
    headers=github_headers(),
    timeout=30,
)
    except requests.exceptions.Timeout:
        raise Exception("GitHub API timed out. Check your internet connection and try again.")
    except requests.exceptions.ConnectionError:
        raise Exception("Could not connect to GitHub API. Check your internet connection and try again.")

    if response.status_code == 401:
        raise Exception("GitHub authentication failed. Check your GITHUB_TOKEN.")

    if response.status_code == 403:
        raise Exception("GitHub API rate limit exceeded.")

    if response.status_code != 200:
        raise Exception(
        f"GitHub API error ({response.status_code})."
    )

    return response.json()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")


def github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "BashOps-Radar",
    }

    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    return headers


def days_since(date_string: str) -> int:
    if not date_string:
        return 999
    dt = datetime.fromisoformat(date_string.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).days


def classify_issue(issue):
    title = issue.get("title", "").lower()
    labels = " ".join(label.get("name", "").lower() for label in issue.get("labels", []))
    text = f"{title} {labels}"

    if "good first issue" in text or "beginner" in text:
        return "Good First Issue", 90
    if any(word in text for word in ["bug", "error", "fail", "fix", "broken"]):
        return "Bug Fix", 85
    if any(word in text for word in ["test", "ci", "workflow"]):
        return "Testing/CI", 75
    if any(word in text for word in ["docs", "documentation"]):
        return "Docs", 60
    if any(word in text for word in ["feature", "enhancement", "fr"]):
        return "Feature", 55
    return "General", 50


def score_issue(issue):
    issue_type, score = classify_issue(issue)

    if issue.get("comments", 0) >= 1:
        score += 10

    updated_days = days_since(issue.get("updated_at"))
    created_days = days_since(issue.get("created_at"))

    if updated_days <= 7:
        score += 15
    elif updated_days <= 30:
        score += 8

    if created_days <= 30:
        score += 10
    elif created_days > 365:
        score -= 20

    return min(max(score, 0), 100), issue_type


def score_repo(repo, issues, languages):
    score = 0
    open_issues = repo.get("open_issues_count", 0)
    stars = repo.get("stargazers_count", 0)
    forks = repo.get("forks_count", 0)
    last_push_days = days_since(repo.get("pushed_at"))

    if 5 <= open_issues <= 80:
        score += 25
    elif open_issues > 80:
        score += 10
    elif open_issues > 0:
        score += 12

    if last_push_days <= 3:
        score += 25
    elif last_push_days <= 14:
        score += 18
    elif last_push_days <= 30:
        score += 10

    if 20 <= stars <= 5000:
        score += 20
    elif stars > 5000:
        score += 10
    elif stars > 0:
        score += 8

    if 2 <= forks <= 500:
        score += 10
    elif forks > 500:
        score += 5

    if languages:
        score += 10

    if any(lang in languages for lang in ["Python", "TypeScript", "JavaScript"]):
        score += 10

    issue_scores = [score_issue(issue)[0] for issue in issues]
    if issue_scores:
        avg = sum(issue_scores) / len(issue_scores)
        if avg >= 75:
            score += 15
        elif avg >= 60:
            score += 8

    return min(score, 100)


def decision(score):
    if score >= 80:
        return "YES — strong Proof-of-Work target"
    if score >= 60:
        return "MAYBE — inspect manually before committing time"
    return "NO — low probability target for now"
def primary_language(languages):
    if not languages:
        return "Unknown"

    top_language = max(languages, key=languages.get)

    badges = {
        "Python": "🐍 Python",
        "TypeScript": "⚡ TypeScript",
        "JavaScript": "🟨 JavaScript",
        "Rust": "🦀 Rust",
        "Go": "🐹 Go",
        "Java": "☕ Java",
        "PHP": "🐘 PHP",
        "Ruby": "💎 Ruby",
        "C++": "⚙️ C++",
        "C": "⚙️ C",
    }

    return badges.get(top_language, top_language)

def recommend_angle(languages):
    if "Python" in languages:
        return "Backend reliability, API validation, async workflows, database/session handling."
    if "TypeScript" in languages or "JavaScript" in languages:
        return "Developer experience, Git workflow reliability, frontend/backend integration, API edge cases."
    return "Small scoped bugs, docs gaps, tests, or integration reliability."


def get_analysis(repo_url: str):
    owner, repo_name = parse_github_url(repo_url)

    repo = github_get(f"/repos/{owner}/{repo_name}")
    issues_raw = github_get(f"/repos/{owner}/{repo_name}/issues?state=open&per_page=30")
    languages = github_get(f"/repos/{owner}/{repo_name}/languages")

    issues = [issue for issue in issues_raw if "pull_request" not in issue]

    issue_rankings = []
    for issue in issues:
        issue_score, issue_type = score_issue(issue)
        issue_rankings.append((issue_score, issue_type, issue))

    issue_rankings.sort(key=lambda item: item[0], reverse=True)
    repo_score = score_repo(repo, issues, languages)
    language_badge = primary_language(languages)

    return owner, repo_name, repo, languages, issue_rankings, repo_score, language_badge


def export_markdown_report(owner, repo_name, repo, languages, issue_rankings, repo_score):
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)

    filename = reports_dir / f"{owner}_{repo_name}_report.md"
    best_issue = issue_rankings[0][2] if issue_rankings else None

    issues_md = ""
    for issue_score, issue_type, issue in issue_rankings[:10]:
        issues_md += f"""
### Issue #{issue.get("number")} — {issue.get("title")}
- Score: {issue_score}/100
- Type: {issue_type}
- Updated: {issue.get("updated_at", "")[:10]}
- URL: {issue.get("html_url")}
"""

    languages_md = "\n".join(f"- {lang}: {value}" for lang, value in languages.items())

    content = f"""# BashOps Radar Opportunity Report

## Company / Repository
**Repo:** {owner}/{repo_name}  
**Website:** {repo.get("homepage") or "Not found"}  
**GitHub:** {repo.get("html_url")}  
**Issues:** {repo.get("html_url")}/issues  

## Why This Company
This repository shows active engineering activity, open technical issues, and a codebase that matches backend/API/AI infrastructure Proof-of-Work opportunities.

## Why Now
- Last push: {repo.get("pushed_at")}
- Open issues: {repo.get("open_issues_count")}
- Stars: {repo.get("stargazers_count")}
- Forks: {repo.get("forks_count")}
- Opportunity score: {repo_score}/100
- Decision: {decision(repo_score)}

## Best First Target
{f'Issue #{best_issue.get("number")}: {best_issue.get("title")}  ' if best_issue else 'No issue found.'}
{f'URL: {best_issue.get("html_url")}' if best_issue else ''}

## Suggested Proof-of-Work Angle
{recommend_angle(languages)}

## Paid Sprint Angle
Offer a 48-hour sprint to fix 1–2 scoped backend/API reliability issues, add tests where possible, and provide a clean technical summary.

## Founder Message
Hi, I reviewed {repo_name} and noticed active issues that match my backend/API reliability work.

I specialize in backend reliability, API integrations, and production-focused fixes.

I can take one scoped issue, submit a clean PR, and if useful, we can discuss a short paid sprint afterward.

## Languages
{languages_md}

## Top Ranked Issues
{issues_md}
"""

    filename.write_text(content, encoding="utf-8")
    return filename


def save_target(owner, repo_name, repo_score, issue_rankings):
    best_issue = issue_rankings[0][2] if issue_rankings else None
    file_exists = TARGETS_FILE.exists()
    

def print_header():
    console.print(f"\n[bold green]{APP_NAME} v{APP_VERSION}[/bold green]")
    console.print("[cyan]AI Opportunity Intelligence for Developers[/cyan]\n")


def today_briefing():
    print_header()

    target = best_pitch_target()
    if not target:
        return

    repo = target.get("repo", "Unknown")
    score = target.get("score_int", target.get("score", "0"))
    best_issue = target.get("best_issue", "N/A")
    status = target.get("status", "N/A")
    next_action = target.get("next_action", "N/A")
    url = target.get("url", "N/A")

    console.print("[bold yellow]Today's Best Opportunity[/bold yellow]")
    console.print(f"[bold]Company / Repo:[/bold] {repo}")
    console.print(f"[bold]Score:[/bold] {score}/100")
    console.print(f"[bold]Best Issue:[/bold] {best_issue}")
    console.print(f"[bold]Status:[/bold] {status}")
    console.print(f"[bold]Next Action:[/bold] {next_action}")
    console.print(f"[bold]URL:[/bold] {url}")

    console.print("\n[bold green]Recommended Focus:[/bold green]")
    console.print("Work on the highest active opportunity. Keep scope small, submit one clean PR, then pitch only after trust is built.")


def list_targets():
    if not TARGETS_FILE.exists():
        console.print("[yellow]No targets saved yet.[/yellow]")
        return

    table = Table(title="BashOps Radar Targets")
    table.add_column("Repo")
    table.add_column("Score")
    table.add_column("Best Issue")
    table.add_column("Status")
    table.add_column("Next Action")
    table.add_column("URL")

    with TARGETS_FILE.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            table.add_row(
                row["repo"],
                row["score"],
                row["best_issue"],
                row["status"],
                row["next_action"],
                row["url"],
            )

    console.print(table)

def target_priority(score: int) -> str:
    if score >= 90:
        return "HOT"
    if score >= 75:
        return "WARM"
    return "WATCH"


def rank_targets():
    if not TARGETS_FILE.exists():
        console.print("[yellow]No targets saved yet.[/yellow]")
        return []

    with TARGETS_FILE.open("r", encoding="utf-8") as f:
        targets = list(csv.DictReader(f))

    for target in targets:
        try:
            target["score_int"] = int(target.get("score", 0))
        except ValueError:
            target["score_int"] = 0

        target["priority"] = target_priority(target["score_int"])

    return sorted(targets, key=lambda row: row["score_int"], reverse=True)


def pipeline_report():
    ranked = rank_targets()
    if not ranked:
        return

    table = Table(title="BashOps Radar Pipeline Ranking")
    table.add_column("Priority")
    table.add_column("Repo")
    table.add_column("Score")
    table.add_column("Best Issue")
    table.add_column("Status")
    table.add_column("Next Action")
    table.add_column("URL")

    for target in ranked:
        table.add_row(
            target["priority"],
            target["repo"],
            str(target["score_int"]),
            target["best_issue"],
            target["status"],
            target["next_action"],
            target["url"],
        )

    console.print(table)

    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    filename = reports_dir / "pipeline_report.md"

    rows = []
    for index, target in enumerate(ranked, start=1):
        rows.append(
            f"""## {index}. {target["repo"]} — {target["priority"]}

- Score: {target["score_int"]}/100
- Best issue: {target["best_issue"]}
- Status: {target["status"]}
- Next action: {target["next_action"]}
- URL: {target["url"]}
"""
        )

    content = f"""# BashOps Radar Pipeline Report

Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}

## Decision Rule

- HOT: work on this next
- WARM: inspect manually
- WATCH: keep in backlog

{"".join(rows)}
"""

    filename.write_text(content, encoding="utf-8")
    console.print(f"\n[bold green]Pipeline report exported:[/bold green] {filename}")

def estimate_difficulty(issue_type: str, issue_score: int) -> str:
    if issue_type in ["Docs", "Good First Issue"]:
        return "Low"
    if issue_type in ["Bug Fix", "Testing/CI"] and issue_score >= 80:
        return "Medium"
    return "Medium/High"


def estimate_time(issue_type: str) -> str:
    if issue_type == "Docs":
        return "30–60 minutes"
    if issue_type == "Good First Issue":
        return "1–2 hours"
    if issue_type == "Testing/CI":
        return "2–4 hours"
    if issue_type == "Bug Fix":
        return "3–6 hours"
    return "4–8 hours"


def merge_probability(issue_score: int, repo_score: int) -> str:
    average = (issue_score + repo_score) / 2

    if average >= 85:
        return "High"
    if average >= 70:
        return "Medium"
    return "Low"


def enrich_repo(repo_url: str):
    owner, repo_name, repo, languages, issue_rankings, repo_score, language_badge = get_analysis(repo_url)

    console.print("\n[bold green]BashOps Radar Founder Intelligence V0.8[/bold green]")
    console.print(f"[bold]Repo:[/bold] {owner}/{repo_name}")
    console.print(f"[bold]Website:[/bold] {repo.get('homepage') or 'Not found'}")
    console.print(f"[bold]GitHub:[/bold] {repo.get('html_url')}")
    console.print(f"[bold]Owner / Org:[/bold] {owner}")
    console.print(f"[bold]Description:[/bold] {repo.get('description')}")
    console.print(f"[bold]Stars:[/bold] {repo.get('stargazers_count')}")
    console.print(f"[bold]Open Issues:[/bold] {repo.get('open_issues_count')}")
    console.print(f"[bold]Last Push:[/bold] {repo.get('pushed_at')}")
    console.print(f"[bold]Opportunity Score:[/bold] {repo_score}/100")
    console.print(f"[bold]Language:[/bold] {language_badge}")
    console.print(f"[bold]Decision:[/bold] {decision(repo_score)}")

    console.print("\n[bold yellow]Founder / Company Intelligence:[/bold yellow]")
    console.print("- Founder/contact lookup: Manual verification required")
    console.print("- Hiring signal: Check website careers page, README, GitHub org, and LinkedIn")
    console.print("- Funding signal: Check YC, Crunchbase, Wellfound, company blog, or launch posts")
    console.print("- Best outreach path: GitHub interaction first, then founder email/LinkedIn after useful PR")

    console.print("\n[bold cyan]Business Opportunity Notes:[/bold cyan]")
    console.print("This target is useful if the repo is active, founder/maintainer responds, and issues match backend/API reliability work.")


def plan_opportunity(repo_url: str):
    owner, repo_name, repo, languages, issue_rankings, repo_score, language_badge = get_analysis(repo_url)

    console.print("\n[bold green]BashOps Radar Opportunity Planner V0.9[/bold green]")
    console.print(f"[bold]Repo:[/bold] {owner}/{repo_name}")
    console.print(f"[bold]Opportunity Score:[/bold] {repo_score}/100")
    console.print(f"[bold]Decision:[/bold] {decision(repo_score)}")

    if not issue_rankings:
        console.print("[yellow]No open issues found to plan around.[/yellow]")
        return

    best_score, best_type, best_issue = issue_rankings[0]

    console.print("\n[bold cyan]Best Issue:[/bold cyan]")
    console.print(f"Issue #{best_issue.get('number')}: {best_issue.get('title')}")
    console.print(f"URL: {best_issue.get('html_url')}")
    console.print(f"Type: {best_type}")
    console.print(f"Issue Score: {best_score}/100")

    console.print("\n[bold yellow]Execution Plan:[/bold yellow]")
    console.print(f"Difficulty: {estimate_difficulty(best_type, best_score)}")
    console.print(f"Estimated Time: {estimate_time(best_type)}")
    console.print(f"Merge Probability: {merge_probability(best_score, repo_score)}")
    console.print(f"Suggested Angle: {recommend_angle(languages)}")

    console.print("\n[bold cyan]PR Strategy:[/bold cyan]")
    console.print("1. Reproduce or inspect the issue.")
    console.print("2. Make the smallest useful fix.")
    console.print("3. Add or update tests if practical.")
    console.print("4. Keep the PR focused and easy to review.")
    console.print("5. After review/merge, pitch a small paid sprint only if trust is built.")

    console.print("\n[bold green]Sprint Opportunity:[/bold green]")
    console.print("Offer: 48-hour backend/API reliability sprint fixing 1–3 scoped issues with tests and summary.")

def best_pitch_target():
    ranked = rank_targets()
    if not ranked:
        return None

    inactive_statuses = {"closed", "merged", "won", "lost", "do not pitch"}

    active_targets = [
        target for target in ranked
        if target.get("status", "").strip().lower() not in inactive_statuses
        and target.get("next_action", "").strip().lower() != "do not pitch"
    ]

    if not active_targets:
        console.print("[yellow]No active pitch targets found.[/yellow]")
        return None

    return active_targets[0]


def pitch_engine():
    target = best_pitch_target()
    if not target:
        return

    repo = target.get("repo", "Unknown")
    best_issue = target.get("best_issue", "N/A")
    score = target.get("score_int", target.get("score", "0"))
    url = target.get("url", "")

    console.print("\n[bold green]BashOps Radar Contract Engine V1.0[/bold green]")
    console.print(f"[bold]Best Target:[/bold] {repo}")
    console.print(f"[bold]Score:[/bold] {score}/100")
    console.print(f"[bold]Best Issue:[/bold] {best_issue}")
    console.print(f"[bold]Issue URL:[/bold] {url}")

    console.print("\n[bold yellow]Recommended Offer:[/bold yellow]")
    console.print("48-hour backend/API reliability sprint fixing 1–3 scoped issues with tests and a clear technical summary.")

    console.print("\n[bold cyan]Founder Message:[/bold cyan]")
    console.print(
        f"Hi, I reviewed {repo} and noticed {best_issue} plus a few related backend/API reliability areas.\n\n"
        f"I specialize in Python/FastAPI, API reliability, async workflows, and production-focused fixes.\n\n"
        f"I can take 1–3 scoped issues in a short 48-hour sprint, submit clean PRs with tests where practical, "
        f"and provide a concise technical summary.\n\n"
        f"If useful, I’d be happy to start with a small fixed sprint."
    )

    console.print("\n[bold cyan]Follow-up Message:[/bold cyan]")
    console.print(
        f"Hi, just following up on my note about helping with {repo}.\n\n"
        f"I noticed there are still scoped engineering issues that match my backend/API reliability work. "
        f"Happy to take a small sprint and keep it focused on practical fixes."
    )

    console.print("\n[bold green]Next Action:[/bold green]")
    console.print("Use this only after trust is built: maintainer reply, review, approval, or merged PR.")

def analyze_repo(repo_url: str, save=False):
    owner, repo_name, repo, languages, issue_rankings, repo_score, language_badge = get_analysis(repo_url)

    console.print("\n[bold green]BashOps Radar Report V0.6[/bold green]")
    console.print(f"[bold]Repo:[/bold] {owner}/{repo_name}")
    console.print(f"[bold]Website:[/bold] {repo.get('homepage') or 'Not found'}")
    console.print(f"[bold]GitHub:[/bold] {repo.get('html_url')}")
    console.print(f"[bold]Issues:[/bold] {repo.get('html_url')}/issues")
    console.print(f"[bold]Description:[/bold] {repo.get('description')}")
    console.print(f"[bold]Stars:[/bold] {repo.get('stargazers_count')}")
    console.print(f"[bold]Forks:[/bold] {repo.get('forks_count')}")
    console.print(f"[bold]Open Issues:[/bold] {repo.get('open_issues_count')}")
    console.print(f"[bold]Last Push:[/bold] {repo.get('pushed_at')}")
    console.print(f"[bold]Opportunity Score:[/bold] {repo_score}/100")
    console.print(f"[bold]Decision:[/bold] {decision(repo_score)}\n")

    issue_table = Table(title="Top Ranked Open Issues")
    issue_table.add_column("#")
    issue_table.add_column("Score")
    issue_table.add_column("Type")
    issue_table.add_column("Title")
    issue_table.add_column("Updated")
    issue_table.add_column("URL")

    for issue_score, issue_type, issue in issue_rankings[:10]:
        issue_table.add_row(
            str(issue.get("number")),
            str(issue_score),
            issue_type,
            issue.get("title", "")[:70],
            issue.get("updated_at", "")[:10],
            issue.get("html_url", ""),
        )

    console.print(issue_table)

    console.print("\n[bold yellow]Suggested Proof-of-Work Angle:[/bold yellow]")
    console.print(recommend_angle(languages))

    if issue_rankings:
        best_score, best_type, best_issue = issue_rankings[0]
        console.print("\n[bold cyan]Best First Target:[/bold cyan]")
        console.print(f"Issue #{best_issue.get('number')}: {best_issue.get('title')}")
        console.print(f"Type: {best_type}")
        console.print(f"Score: {best_score}/100")
        console.print(f"URL: {best_issue.get('html_url')}")

    console.print("\n[bold cyan]Founder Pitch Angle:[/bold cyan]")
    console.print(
        f"Hi, I reviewed {repo_name} and noticed active issues that match my backend/API reliability work. "
        f"I can take one scoped issue, submit a clean PR, and then discuss a short paid sprint if useful."
    )

    report_path = export_markdown_report(owner, repo_name, repo, languages, issue_rankings, repo_score)
    console.print(f"\n[bold green]Report exported:[/bold green] {report_path}")

    if save:
        save_target(owner, repo_name, repo_score, issue_rankings)
        console.print("[bold green]Target saved to targets.csv[/bold green]")


def help_text():
    console.print("""
[bold green]BashOps Radar Commands[/bold green]

Analyze repo:
  python radar.py analyze https://github.com/aegra/aegra

Analyze and save target:
  python radar.py add https://github.com/aegra/aegra

List saved targets:
  python radar.py list

Today's briefing:
  python radar.py today

Contract engine:
  python radar.py pitch

Contract engine:
  python radar.py pitch

Founder intelligence:
  python radar.py enrich https://github.com/sourcebot-dev/sourcebot

Opportunity planner:
  python radar.py plan https://github.com/sourcebot-dev/sourcebot

Rank saved pipeline:
  python radar.py pipeline
""")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        help_text()
        sys.exit(0)

    command = sys.argv[1]

    if command == "analyze" and len(sys.argv) > 2:
        analyze_repo(sys.argv[2], save=False)
    elif command == "pipeline":
         pipeline_report()
    elif command == "add" and len(sys.argv) >= 3:
        analyze_repo(sys.argv[2], save=True)
    elif command == "list":
        list_targets()
    elif command == "enrich" and len(sys.argv) >= 3:
         enrich_repo(sys.argv[2])
    elif command == "plan" and len(sys.argv) >= 3:
         plan_opportunity(sys.argv[2])
    elif command == "pitch":
         pitch_engine()
    elif command == "today":
        today_briefing()
    elif command == "pitch":
        pitch_engine()
    else:
        # backward compatibility
        if "github.com" in command:
            analyze_repo(command, save=False)
        else:
            help_text()
