import sys
import requests
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


def score_repo(repo, issues, languages):
    score = 0

    if repo.get("stargazers_count", 0) >= 50:
        score += 15

    if repo.get("open_issues_count", 0) >= 5:
        score += 20

    if repo.get("pushed_at"):
        score += 20

    if languages:
        score += 15

    if repo.get("forks_count", 0) >= 5:
        score += 10

    if len(issues) >= 5:
        score += 20

    return min(score, 100)


def analyze_repo(repo_url: str):
    owner, repo_name = parse_github_url(repo_url)

    repo = github_get(f"/repos/{owner}/{repo_name}")
    issues = github_get(f"/repos/{owner}/{repo_name}/issues?state=open&per_page=10")
    languages = github_get(f"/repos/{owner}/{repo_name}/languages")

    score = score_repo(repo, issues, languages)

    console.print(f"\n[bold green]BashOps Radar Report[/bold green]")
    console.print(f"[bold]Repo:[/bold] {owner}/{repo_name}")
    console.print(f"[bold]Description:[/bold] {repo.get('description')}")
    console.print(f"[bold]Stars:[/bold] {repo.get('stargazers_count')}")
    console.print(f"[bold]Forks:[/bold] {repo.get('forks_count')}")
    console.print(f"[bold]Open Issues:[/bold] {repo.get('open_issues_count')}")
    console.print(f"[bold]Last Push:[/bold] {repo.get('pushed_at')}")
    console.print(f"[bold]Opportunity Score:[/bold] {score}/100\n")

    lang_table = Table(title="Languages")
    lang_table.add_column("Language")
    lang_table.add_column("Bytes")

    for lang, value in languages.items():
        lang_table.add_row(lang, str(value))

    console.print(lang_table)

    issue_table = Table(title="Recent Open Issues")
    issue_table.add_column("#")
    issue_table.add_column("Title")
    issue_table.add_column("URL")

    for issue in issues[:10]:
        if "pull_request" not in issue:
            issue_table.add_row(
                str(issue.get("number")),
                issue.get("title", "")[:80],
                issue.get("html_url", "")
            )

    console.print(issue_table)

    console.print("\n[bold yellow]Suggested Proof-of-Work Angle:[/bold yellow]")

    if "Python" in languages:
        console.print("Backend reliability, API validation, async workflows, or infrastructure fixes.")
    elif "TypeScript" in languages or "JavaScript" in languages:
        console.print("Developer experience, frontend/backend integration, GitHub workflows, or API edge cases.")
    else:
        console.print("Look for small open issues with clear reproduction steps and maintainer activity.")

    console.print("\n[bold cyan]Founder Pitch Angle:[/bold cyan]")
    console.print(
        f"Hi, I reviewed {repo_name} and noticed active open issues around the codebase. "
        f"I specialize in backend/API reliability and can take one small issue, submit a clean PR, "
        f"and then discuss a short paid sprint if useful."
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        console.print("[red]Usage:[/red] python radar.py <github_repo_url>")
        sys.exit(1)

    analyze_repo(sys.argv[1])