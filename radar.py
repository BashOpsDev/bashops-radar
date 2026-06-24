import sys
from pathlib import Path
import requests
from datetime import datetime, timezone
from rich.console import Console
from rich.table import Table

console = Console()


def parse_github_url(url: str):
    parts = url.rstrip("/").split("/")
    if "github.com" not in url or len(parts) < 5:
        raise ValueError("Please provide a valid GitHub repo URL.")
    return parts[-2], parts[-1]


def github_get(endpoint: str):
    url = f"https://api.github.com{endpoint}"
    response = requests.get(url, timeout=20)

    if response.status_code != 200:
        raise Exception(f"GitHub API error: {response.status_code} - {response.text}")

    return response.json()


def days_since(date_string: str) -> int:
    if not date_string:
        return 999

    dt = datetime.fromisoformat(date_string.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return (now - dt).days


def classify_issue(issue):
    title = issue.get("title", "").lower()
    labels = [label.get("name", "").lower() for label in issue.get("labels", [])]

    text = " ".join([title] + labels)

    if "good first issue" in text or "beginner" in text:
        return "Good First Issue", 90

    if "bug" in text or "error" in text or "fail" in text or "fix" in text:
        return "Bug Fix", 85

    if "docs" in text or "documentation" in text:
        return "Docs", 60

    if "test" in text or "ci" in text:
        return "Testing/CI", 75

    if "feature" in text or "enhancement" in text:
        return "Feature", 55

    return "General", 50


def score_issue(issue):
    issue_type, base_score = classify_issue(issue)

    comments = issue.get("comments", 0)
    age_days = days_since(issue.get("created_at"))
    updated_days = days_since(issue.get("updated_at"))

    score = base_score

    if comments >= 1:
        score += 10

    if updated_days <= 7:
        score += 15
    elif updated_days <= 30:
        score += 8

    if age_days <= 30:
        score += 10
    elif age_days > 365:
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

    good_issues = [score_issue(issue)[0] for issue in issues if "pull_request" not in issue]
    if good_issues:
        avg_issue_score = sum(good_issues) / len(good_issues)
        if avg_issue_score >= 75:
            score += 15
        elif avg_issue_score >= 60:
            score += 8

    return min(score, 100)


def decision(score):
    if score >= 80:
        return "YES — strong Proof-of-Work target"
    if score >= 60:
        return "MAYBE — inspect manually before committing time"
    return "NO — low probability target for now"


def recommend_angle(languages):
    if "Python" in languages:
        return "Backend reliability, API validation, async workflows, database/session handling."
    if "TypeScript" in languages or "JavaScript" in languages:
        return "Developer experience, Git workflow reliability, frontend/backend integration, API edge cases."
    return "Small scoped bugs, docs gaps, tests, or integration reliability."

def export_markdown_report(owner, repo_name, repo, languages, issue_rankings, repo_score):
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)

    filename = reports_dir / f"{owner}_{repo_name}_report.md"

    best_issue_block = "No open issues found."

    if issue_rankings:
        best_score, best_type, best_issue = issue_rankings[0]
        best_issue_block = f"""
## Best First Target

Issue #{best_issue.get("number")}: {best_issue.get("title")}

Type: {best_type}

Score: {best_score}/100

URL: {best_issue.get("html_url")}
"""

    issues_md = ""

    for issue_score, issue_type, issue in issue_rankings[:10]:
        issues_md += f"""
### Issue #{issue.get("number")} — {issue.get("title")}

- Score: {issue_score}/100
- Type: {issue_type}
- Updated: {issue.get("updated_at", "")[:10]}
- URL: {issue.get("html_url")}
"""

    languages_md = "\n".join([f"- {lang}: {value}" for lang, value in languages.items()])

    content = f"""# BashOps Radar Report

## Repository

**Repo:** {owner}/{repo_name}

**Description:** {repo.get("description")}

**Stars:** {repo.get("stargazers_count")}

**Forks:** {repo.get("forks_count")}

**Open Issues:** {repo.get("open_issues_count")}

**Last Push:** {repo.get("pushed_at")}

**Opportunity Score:** {repo_score}/100

**Decision:** {decision(repo_score)}

## Languages

{languages_md}

{best_issue_block}

## Top Ranked Issues

{issues_md}

## Suggested Proof-of-Work Angle

{recommend_angle(languages)}

## Founder Pitch Angle

Hi, I reviewed {repo_name} and noticed active issues that match my backend/API reliability work. I can take one scoped issue, submit a clean PR, and then discuss a short paid sprint if useful.
"""

    filename.write_text(content, encoding="utf-8")
    return filename


def analyze_repo(repo_url: str):
    owner, repo_name = parse_github_url(repo_url)

    repo = github_get(f"/repos/{owner}/{repo_name}")
    issues = github_get(f"/repos/{owner}/{repo_name}/issues?state=open&per_page=20")
    languages = github_get(f"/repos/{owner}/{repo_name}/languages")

    real_issues = [issue for issue in issues if "pull_request" not in issue]

    issue_rankings = []
    for issue in real_issues:
        issue_score, issue_type = score_issue(issue)
        issue_rankings.append((issue_score, issue_type, issue))

    issue_rankings.sort(key=lambda item: item[0], reverse=True)

    repo_score = score_repo(repo, real_issues, languages)

    console.print("\n[bold green]BashOps Radar Report V0.2[/bold green]")
    console.print(f"[bold]Repo:[/bold] {owner}/{repo_name}")
    console.print(f"[bold]Description:[/bold] {repo.get('description')}")
    console.print(f"[bold]Stars:[/bold] {repo.get('stargazers_count')}")
    console.print(f"[bold]Forks:[/bold] {repo.get('forks_count')}")
    console.print(f"[bold]Open Issues:[/bold] {repo.get('open_issues_count')}")
    console.print(f"[bold]Last Push:[/bold] {repo.get('pushed_at')}")
    console.print(f"[bold]Opportunity Score:[/bold] {repo_score}/100")
    console.print(f"[bold]Decision:[/bold] {decision(repo_score)}\n")

    lang_table = Table(title="Languages")
    lang_table.add_column("Language")
    lang_table.add_column("Bytes")

    for lang, value in languages.items():
        lang_table.add_row(lang, str(value))

    console.print(lang_table)

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
            issue.get("html_url", "")
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
    report_path = export_markdown_report(
        owner=owner,
        repo_name=repo_name,
        repo=repo,
        languages=languages,
        issue_rankings=issue_rankings,
        repo_score=repo_score,
    )

    console.print(f"\n[bold green]Report exported:[/bold green] {report_path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        console.print("[red]Usage:[/red] python radar.py <github_repo_url>")
        sys.exit(1)

    analyze_repo(sys.argv[1])