import os
import sys
from pathlib import Path
from datetime import datetime, timezone

import requests
from rich.console import Console
from rich.table import Table
from config import APP_NAME, APP_VERSION, PUBLIC_MODE

console = Console()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")


# Repository opportunity scoring is composed from independent, capped evidence
# groups. The methodology page reads this same data so documentation cannot
# drift from the implementation.
OPPORTUNITY_SCORE_COMPONENTS = (
    {"key": "activity", "label": "Repository activity", "weight": 25, "source": "Repository pushed_at", "positive": "Recent pushes increase the component.", "negative": "Missing or stale activity reduces it."},
    {"key": "issue_quality", "label": "Issue quality", "weight": 25, "source": "Up to 30 open GitHub issues", "positive": "Recent, discussed, and contributor-labeled issues increase it.", "negative": "No usable issue sample scores zero."},
    {"key": "contributor_readiness", "label": "Contributor readiness", "weight": 15, "source": "Issue labels and activity", "positive": "Ranked issues and contributor labels increase it.", "negative": "Missing ranked issues or contributor signals reduce it."},
    {"key": "visibility", "label": "Repository visibility", "weight": 10, "source": "GitHub stars", "positive": "Established visibility increases it with diminishing returns.", "negative": "Stars alone cannot produce a strong score."},
    {"key": "community", "label": "Community evidence", "weight": 10, "source": "Forks and sampled issue authors", "positive": "Forks and distinct issue authors increase it.", "negative": "Little observable participation reduces it."},
    {"key": "language", "label": "Language profile", "weight": 5, "source": "GitHub language bytes", "positive": "Available language data and common contribution languages increase it.", "negative": "Missing language data scores zero."},
    {"key": "commercial", "label": "Commercial context", "weight": 5, "source": "Owner type, homepage, and Sponsors metadata", "positive": "Organization and project metadata add limited evidence.", "negative": "These signals never prove paid-work intent."},
    {"key": "completeness", "label": "Evidence completeness", "weight": 5, "source": "Availability of repository, issue, and language metadata", "positive": "More complete public evidence increases confidence in the score.", "negative": "Missing evidence reduces the score and confidence."},
)

RECOMMENDATION_RULES = (
    "Contribute Now requires a score of at least 90, a ranked issue, and High confidence.",
    "Inspect Repository is used when no ranked issue is available.",
    "Inspect Carefully is used for scores from 70 to 89, or when confidence is below High.",
    "Skip is used for scores below 70 when a ranked issue exists.",
)


def parse_github_url(url: str):
    parts = url.rstrip("/").split("/")
    if "github.com" not in url or len(parts) < 5:
        raise ValueError("Please provide a valid GitHub repo URL.")
    return parts[-2], parts[-1]


def github_get(endpoint: str):
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"

    try:
        response = requests.get(
            f"https://api.github.com{endpoint}", headers=headers, timeout=30
        )
    except requests.exceptions.Timeout:
        raise Exception("GitHub API timed out. Check your internet connection and try again.")
    except requests.exceptions.ConnectionError:
        raise Exception("Could not connect to GitHub API. Check your internet connection and try again.")

    if response.status_code == 403 and "rate limit" in response.text.lower():
        raise Exception(
            "GitHub API rate limit reached. Set a GITHUB_TOKEN environment variable "
            "to raise this limit, or try again shortly."
        )

    if response.status_code != 200:
        raise Exception(f"GitHub API error: {response.status_code} - {response.text}")

    return response.json()


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


def _quality_label(value: str, detail: str) -> dict:
    return {"label": value, "detail": detail}


def _days_label(days: int) -> str:
    if days == 0:
        return "today"
    if days == 1:
        return "1 day ago"
    if days >= 999:
        return "unavailable"
    return f"{days} days ago"


def score_repo_signal_report(repo, issues, languages):
    issues = list(issues or [])
    languages = languages or {}
    reasons = []
    warnings = []
    components = []
    stars = int(repo.get("stargazers_count") or 0)
    forks = int(repo.get("forks_count") or 0)
    last_push_days = days_since(repo.get("pushed_at"))
    issue_scores = [score_issue(issue)[0] for issue in issues]
    average_issue_score = sum(issue_scores) / len(issue_scores) if issue_scores else 0
    recent_issue_count = sum(days_since(issue.get("updated_at")) <= 30 for issue in issues)
    discussed_issue_count = sum(int(issue.get("comments") or 0) > 0 for issue in issues)
    contributor_label_count = sum(
        any(
            str(label.get("name") or "").casefold() in {"good first issue", "help wanted"}
            for label in issue.get("labels", [])
        )
        for issue in issues
    )
    distinct_issue_authors = len(
        {
            str((issue.get("user") or {}).get("login") or "").casefold()
            for issue in issues
            if (issue.get("user") or {}).get("login")
        }
    )

    def add_component(key: str, earned: int, detail: str):
        definition = next(item for item in OPPORTUNITY_SCORE_COMPONENTS if item["key"] == key)
        components.append({**definition, "earned": max(0, min(int(earned), definition["weight"])), "detail": detail})

    activity_points = 25 if last_push_days <= 3 else 22 if last_push_days <= 14 else 18 if last_push_days <= 30 else 10 if last_push_days <= 90 else 4 if last_push_days <= 180 else 0
    add_component("activity", activity_points, f"Last push: {_days_label(last_push_days)}")
    if activity_points >= 18:
        reasons.append(_quality_label("Recently maintained", f"Last push: {_days_label(last_push_days)}"))
    else:
        warnings.append(_quality_label("Maintenance evidence is limited", f"Last push: {_days_label(last_push_days)}"))

    issue_quality_points = 0
    if issue_scores:
        issue_quality_points += 14 if average_issue_score >= 90 else 12 if average_issue_score >= 80 else 9 if average_issue_score >= 70 else 6 if average_issue_score >= 60 else 3
        issue_quality_points += 6 if recent_issue_count >= 5 else 4 if recent_issue_count >= 2 else 2 if recent_issue_count else 0
        issue_quality_points += 5 if discussed_issue_count >= 5 else 3 if discussed_issue_count >= 2 else 1 if discussed_issue_count else 0
        reasons.append(_quality_label("Issue evidence available", f"{len(issue_scores)} open issues ranked; average issue score {round(average_issue_score)}"))
    else:
        warnings.append(_quality_label("No ranked issues found", "No usable open issue was available, so issue quality could not be scored"))
    add_component("issue_quality", issue_quality_points, f"{len(issue_scores)} ranked issues, {recent_issue_count} recently updated, {discussed_issue_count} with discussion")

    readiness_points = (4 if issue_scores else 0) + (7 if contributor_label_count >= 3 else 5 if contributor_label_count else 0) + (2 if recent_issue_count else 0) + (2 if discussed_issue_count else 0)
    add_component("contributor_readiness", readiness_points, f"{contributor_label_count} contributor-labeled issues in the sample")
    if contributor_label_count:
        reasons.append(_quality_label("Contributor-ready labels detected", f"{contributor_label_count} sampled issues use good-first-issue or help-wanted labels"))

    visibility_points = 0 if stars <= 0 else 2 if stars < 20 else 4 if stars < 100 else 7 if stars < 1000 else 9 if stars < 5000 else 10
    add_component("visibility", visibility_points, f"{stars} stars, scored with diminishing returns")
    if stars:
        reasons.append(_quality_label("Repository visibility", f"{stars} stars; popularity is only one capped signal"))
    if stars >= 5000:
        warnings.append(_quality_label("High contributor competition", "Large public visibility can increase competition for maintainer attention"))

    fork_points = 0 if forks <= 0 else 2 if forks == 1 else 4 if forks < 10 else 6 if forks < 100 else 8
    author_points = 2 if distinct_issue_authors >= 5 else 1 if distinct_issue_authors >= 2 else 0
    add_component("community", fork_points + author_points, f"{forks} forks and {distinct_issue_authors} distinct authors in the issue sample")

    target_languages = [lang for lang in ("Python", "TypeScript", "JavaScript") if lang in languages]
    language_points = (3 if languages else 0) + (2 if target_languages else 0)
    add_component("language", language_points, ", ".join(list(languages.keys())[:3]) if languages else "Language data unavailable")
    if languages:
        reasons.append(_quality_label("Language profile available", ", ".join(list(languages.keys())[:3])))
    else:
        warnings.append(_quality_label("Language signal unavailable", "GitHub did not return language data"))

    owner_type = str((repo.get("owner") or {}).get("type") or "").casefold()
    commercial_points = (3 if owner_type == "organization" else 0) + (1 if repo.get("homepage") else 0) + (1 if repo.get("has_sponsors") else 0)
    add_component("commercial", commercial_points, "Organization, homepage, and Sponsors metadata only; paid intent is not inferred")

    core_fields = ("pushed_at", "open_issues_count", "stargazers_count", "forks_count")
    completeness_points = (2 if all(repo.get(field) is not None for field in core_fields) else 1) + (1 if issues else 0) + (1 if languages else 0) + (1 if repo.get("description") or repo.get("license") or repo.get("homepage") else 0)
    add_component("completeness", completeness_points, "Public repository, issue, and language evidence available")

    warnings.append(_quality_label("Review speed unavailable", "Issue metadata does not include first-response timelines, so maintainer response speed was not scored"))
    signals_used = [
        _quality_label("Repository Activity", "Excellent" if activity_points >= 25 else "Strong" if activity_points >= 18 else "Moderate" if activity_points >= 10 else "Limited"),
        _quality_label("Issue Quality", "Excellent" if issue_quality_points >= 23 else "Strong" if issue_quality_points >= 18 else "Moderate" if issue_quality_points >= 10 else "Limited"),
        _quality_label("Contributor Readiness", "Strong" if readiness_points >= 12 else "Moderate" if readiness_points >= 7 else "Limited"),
        _quality_label("Community Evidence", "Strong" if fork_points + author_points >= 8 else "Moderate" if fork_points + author_points >= 4 else "Limited"),
        _quality_label("Competition", "High" if stars >= 5000 or forks >= 500 else "Medium" if stars >= 1000 or forks >= 100 else "Low"),
    ]

    confidence_reasons = []
    confidence_unknowns = ["Maintainer response speed was not measured from issue timelines"]
    evidence_points = 0
    if all(repo.get(field) is not None for field in core_fields):
        evidence_points += 1
        confidence_reasons.append("Core repository metadata is available")
    if last_push_days < 999:
        evidence_points += 1
        confidence_reasons.append("Repository activity date is available")
    else:
        confidence_unknowns.append("Repository push recency is unavailable")
    if last_push_days <= 30:
        evidence_points += 1
        confidence_reasons.append("Recent repository activity was observed")
    elif last_push_days > 90:
        confidence_unknowns.append("Current maintenance cadence is uncertain")
    if issue_scores:
        evidence_points += 2
        confidence_reasons.append("Ranked issue candidates are available")
        if average_issue_score >= 60:
            evidence_points += 1
            confidence_reasons.append("The sampled issue queue contains usable candidates")
    else:
        confidence_unknowns.append("No ranked issue was available")
    if contributor_label_count:
        evidence_points += 1
        confidence_reasons.append("Contributor-friendly issue labels were observed")
    if languages:
        evidence_points += 1
        confidence_reasons.append("Language profile is available")
    else:
        confidence_unknowns.append("Language profile is unavailable")
    if commercial_points:
        evidence_points += 1
        confidence_reasons.append("Limited organization or project metadata is available")

    confidence = "High" if evidence_points >= 7 and len(confidence_unknowns) <= 1 else "Medium" if evidence_points >= 4 and len(confidence_unknowns) <= 3 else "Low"
    return {
        "score": min(sum(component["earned"] for component in components), 100),
        "components": components,
        "reasons": reasons[:7],
        "warnings": warnings[:5],
        "signals_used": signals_used,
        "confidence": confidence,
        "confidence_reasons": confidence_reasons[:6],
        "confidence_unknowns": confidence_unknowns[:5],
    }


def score_repo(repo, issues, languages):
    return score_repo_signal_report(repo, issues, languages)["score"]


def recommendation(score, best_issue=None, confidence="Medium"):
    """Return the single recommendation used by every Radar surface."""
    if not best_issue:
        return {
            "label": "INSPECT REPOSITORY",
            "decision": "Inspect Repository",
            "explanation": "No ranked open issue was available, so inspect the issue queue before committing time.",
        }
    if score >= 90 and confidence == "High":
        return {
            "label": "CONTRIBUTE NOW",
            "decision": "Contribute Now",
            "explanation": "The repository has strong public evidence, a ranked issue, and High analysis confidence.",
        }
    if score >= 70:
        return {
            "label": "INSPECT CAREFULLY",
            "decision": "Inspect Carefully",
            "explanation": "Useful signals are present, but scope or evidence should be verified before starting work.",
        }
    return {
        "label": "SKIP",
        "decision": "Skip",
        "explanation": "The available public evidence is not strong enough to justify prioritizing this repository now.",
    }


def decision(score, best_issue=True, confidence=None):
    confidence = confidence or ("High" if score >= 90 else "Medium")
    result = recommendation(score, best_issue=best_issue, confidence=confidence)
    return f"{result['decision']} - {result['explanation']}"


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
    if repo.get("private") is True:
        raise ValueError("Private repositories are not supported.")

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
    best_score = issue_rankings[0][0] if issue_rankings else 0
    best_type = issue_rankings[0][1] if issue_rankings else ""
    repo = f"{owner}/{repo_name}"

    from database import SessionLocal
    from models import Target

    db = SessionLocal()
    try:
        target = Target(
            repo=repo,
            repo_url=f"https://github.com/{repo}",
            score=float(repo_score or 0),
            status="New Target",
            best_issue=f"#{best_issue.get('number')}" if best_issue else "",
            best_issue_url=best_issue.get("html_url", "") if best_issue else "",
            merge_probability="High" if repo_score >= 85 else "Medium" if repo_score >= 60 else "Low",
            difficulty=estimate_difficulty(best_type, best_score) if best_issue else "Medium/High",
            estimated_time="2-4 hours" if repo_score >= 85 else "4-8 hours",
            ip_address="cli",
        )
        db.add(target)
        db.commit()
    finally:
        db.close()


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
    targets = rank_targets()
    if not targets:
        console.print("[yellow]No targets saved yet.[/yellow]")
        return

    table = Table(title="BashOps Radar Targets")
    table.add_column("Repo")
    table.add_column("Score")
    table.add_column("Best Issue")
    table.add_column("Status")
    table.add_column("Next Action")
    table.add_column("URL")

    for row in targets:
        table.add_row(
            row["repo"],
            str(row["score_int"]),
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
    from analytics import analytics_summary

    targets = analytics_summary(user_id=None)["rows"]

    for target in targets:
        try:
            target["score_int"] = int(target.get("score", 0))
        except ValueError:
            target["score_int"] = 0

        target["priority"] = target_priority(target["score_int"])
        target["next_action"] = _next_action_for_status(target.get("status", "New Target"))
        target["url"] = target.get("links", {}).get("repo_url", "")

    return sorted(targets, key=lambda row: row["score_int"], reverse=True)


def _next_action_for_status(status: str) -> str:
    actions = {
        "New Target": "Research the issue and confirm it is safe to work on.",
        "Researching": "Read the codebase and prepare a small fix plan.",
        "Working": "Finish the fix and add tests where practical.",
        "PR Submitted": "Monitor review comments and respond quickly.",
        "PR Merged": "Prepare founder outreach.",
        "Founder Contacted": "Follow up with a focused sprint offer.",
        "Paid Sprint": "Deliver the sprint and document results.",
        "Retainer": "Maintain relationship and look for retainer opportunities.",
    }
    return actions.get(status, actions["New Target"])


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
        console.print("[bold green]Target saved to database[/bold green]")


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
    else:
        # backward compatibility
        if "github.com" in command:
            analyze_repo(command, save=False)
        else:
            help_text()
