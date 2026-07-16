import os
from datetime import datetime, timedelta, timezone

import requests


class DiscoveryError(Exception):
    """Safe user-facing discovery error."""


GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

DISCOVERY_CATEGORIES = [
    {
        "value": "python-fastapi",
        "label": "Python / FastAPI",
        "term": "FastAPI",
        "label_query": "help wanted",
        "language": "Python",
        "keywords": ["fastapi", "python", "api", "backend"],
    },
    {
        "value": "ai-infrastructure",
        "label": "AI Infrastructure",
        "term": "AI infrastructure",
        "label_query": "help wanted",
        "language": "Python",
        "keywords": ["ai", "llm", "model", "agent", "inference", "vector"],
    },
    {
        "value": "devtools",
        "label": "DevTools",
        "term": "developer tools cli",
        "label_query": "help wanted",
        "language": "TypeScript",
        "keywords": ["cli", "developer", "tooling", "sdk", "extension"],
    },
    {
        "value": "apis-integrations",
        "label": "APIs / Integrations",
        "term": "api integration",
        "label_query": "help wanted",
        "language": "TypeScript",
        "keywords": ["api", "integration", "webhook", "oauth", "sdk"],
    },
    {
        "value": "testing-ci",
        "label": "Testing / CI",
        "term": "test ci workflow",
        "label_query": "bug",
        "language": "",
        "keywords": ["test", "ci", "workflow", "github actions", "coverage"],
    },
    {
        "value": "docs-quick-wins",
        "label": "Docs Quick Wins",
        "term": "documentation",
        "label_query": "documentation",
        "language": "",
        "keywords": ["docs", "documentation", "readme", "guide", "examples"],
    },
    {
        "value": "good-first-issues",
        "label": "Good First Issues",
        "term": "good first issue",
        "label_query": "good first issue",
        "language": "",
        "keywords": ["good first issue", "beginner", "starter", "help wanted"],
    },
]


def _headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _github_get(path: str, params=None):
    try:
        response = requests.get(
            f"{GITHUB_API}{path}",
            headers=_headers(),
            params=params or {},
            timeout=15,
        )
    except requests.exceptions.Timeout:
        raise DiscoveryError("GitHub discovery timed out. Please try again.")
    except requests.exceptions.ConnectionError:
        raise DiscoveryError("Could not connect to GitHub discovery. Please try again.")
    except requests.exceptions.RequestException:
        raise DiscoveryError("GitHub discovery is temporarily unavailable. Please try again.")

    if response.status_code in (403, 429) and "rate limit" in response.text.lower():
        raise DiscoveryError("GitHub discovery rate limit reached. Please try again shortly.")

    if response.status_code != 200:
        raise DiscoveryError("GitHub discovery is temporarily unavailable. Please try again.")

    return response.json()


def category_options():
    return [{"value": item["value"], "label": item["label"]} for item in DISCOVERY_CATEGORIES]


def _category_by_value(value: str):
    for category in DISCOVERY_CATEGORIES:
        if category["value"] == value:
            return category
    return DISCOVERY_CATEGORIES[0]


def _issue_query(category: dict, label_required: bool) -> str:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
    parts = [
        category["term"],
        "type:issue",
        "state:open",
        f"updated:>{cutoff}",
    ]
    if label_required and category.get("label_query"):
        parts.append(f'label:"{category["label_query"]}"')
    return " ".join(parts)


def _search_issues(category: dict, limit: int):
    items = []
    seen = set()

    for label_required in (True, False):
        if len(items) >= limit:
            break
        payload = _github_get(
            "/search/issues",
            params={
                "q": _issue_query(category, label_required),
                "sort": "updated",
                "order": "desc",
                "per_page": min(20, limit * 3),
            },
        )
        for issue in payload.get("items", []):
            if "pull_request" in issue:
                continue
            url = issue.get("html_url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            items.append(issue)
            if len(items) >= limit:
                break

    return items


def _repo_full_name(issue: dict) -> str:
    repository_url = issue.get("repository_url") or ""
    if "/repos/" in repository_url:
        return repository_url.split("/repos/", 1)[1].strip("/")

    html_url = issue.get("html_url") or ""
    if "github.com/" in html_url:
        parts = html_url.split("github.com/", 1)[1].split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}"

    return ""


def _days_since(date_string: str) -> int:
    if not date_string:
        return 999
    try:
        dt = datetime.fromisoformat(date_string.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except ValueError:
        return 999


def _label_names(issue: dict):
    return [
        (label.get("name") or "").strip()
        for label in issue.get("labels", [])
        if (label.get("name") or "").strip()
    ]


def _score(repo: dict, issue: dict, category: dict) -> int:
    stars = int(repo.get("stargazers_count") or 0)
    open_issues = int(repo.get("open_issues_count") or 0)
    pushed_days = _days_since(repo.get("pushed_at") or "")
    repo_language = (repo.get("language") or "").lower()
    category_language = (category.get("language") or "").lower()
    labels = " ".join(_label_names(issue)).lower()
    title = (issue.get("title") or "").lower()
    text = f"{title} {labels}"

    score = 45
    if pushed_days <= 14:
        score += 14
    elif pushed_days <= 60:
        score += 8

    if 50 <= stars <= 5000:
        score += 12
    elif 10 <= stars < 50:
        score += 6
    elif stars > 5000:
        score += 4

    if 5 <= open_issues <= 300:
        score += 12
    elif open_issues > 300:
        score += 6

    if any(label in text for label in ["help wanted", "good first issue", "bug", "documentation"]):
        score += 12

    if any(keyword in text for keyword in category.get("keywords", [])):
        score += 8

    if category_language and repo_language == category_language:
        score += 6

    if int(issue.get("comments") or 0) <= 5:
        score += 4

    return max(35, min(score, 95))


def _contract_potential(score: int) -> str:
    if score >= 80:
        return "High"
    if score >= 60:
        return "Medium"
    return "Low"


def _why_matched(repo: dict, issue: dict, category: dict):
    stars = int(repo.get("stargazers_count") or 0)
    open_issues = int(repo.get("open_issues_count") or 0)
    pushed_days = _days_since(repo.get("pushed_at") or "")
    repo_language = repo.get("language") or ""
    category_language = category.get("language") or ""
    labels = _label_names(issue)
    lower_labels = " ".join(labels).lower()
    reasons = [f"Matched the {category['label']} preset."]

    if category_language and repo_language == category_language:
        reasons.append(f"Repository language matches {category_language}.")
    if labels:
        reasons.append(f"Issue labels: {', '.join(labels[:3])}.")
    if pushed_days <= 60:
        reasons.append("Repository was updated recently.")
    if open_issues:
        reasons.append(f"{open_issues} open issues indicate an active backlog.")
    if stars >= 50:
        reasons.append(f"{stars} stars show ecosystem visibility.")
    if any(signal in lower_labels for signal in ["help wanted", "good first issue", "documentation", "bug"]):
        reasons.append("Issue has a contributor-friendly label signal.")

    return reasons[:5]


def discover_opportunities(category_value: str, limit: int = 8):
    category = _category_by_value(category_value)
    limit = max(1, min(int(limit or 8), 10))
    issues = _search_issues(category, limit=limit * 2)

    results = []
    seen_repos = set()
    for issue in issues:
        full_name = _repo_full_name(issue)
        if not full_name or full_name in seen_repos:
            continue
        seen_repos.add(full_name)

        try:
            repo = _github_get(f"/repos/{full_name}")
        except DiscoveryError:
            continue

        if repo.get("archived"):
            continue

        score = _score(repo, issue, category)
        results.append(
            {
                "repo": full_name,
                "repo_url": repo.get("html_url") or f"https://github.com/{full_name}",
                "issue_title": issue.get("title") or "Open issue",
                "issue_url": issue.get("html_url") or "",
                "stars": int(repo.get("stargazers_count") or 0),
                "open_issues": int(repo.get("open_issues_count") or 0),
                "last_pushed": repo.get("pushed_at") or "",
                "last_pushed_display": (repo.get("pushed_at") or "")[:10],
                "score": score,
                "contract_potential": _contract_potential(score),
                "why_matched": _why_matched(repo, issue, category),
            }
        )

        if len(results) >= limit:
            break

    return {
        "category": category,
        "results": results,
    }


def discover_candidate_repositories(category_values=None, limit: int = 12):
    """Return a bounded repository candidate pool without assigning a Radar score.

    The daily feed uses this as its cheap discovery stage, then runs the existing
    canonical Radar analysis only for the small set it chooses to persist.
    """
    requested = list(category_values or [item["value"] for item in DISCOVERY_CATEGORIES[:4]])
    selected = [_category_by_value(value) for value in requested[:4]]
    limit = max(1, min(int(limit or 12), 20))
    per_category = max(2, (limit + len(selected) - 1) // max(len(selected), 1))
    candidates = []
    seen = set()

    for category in selected:
        for issue in _search_issues(category, limit=per_category):
            full_name = _repo_full_name(issue)
            normalized = full_name.casefold()
            if not full_name or normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(
                {
                    "repository_full_name": full_name,
                    "repository_url": f"https://github.com/{full_name}",
                    "source_category": category["value"],
                    "source_issue_url": issue.get("html_url") or "",
                }
            )
            if len(candidates) >= limit:
                return candidates

    return candidates
