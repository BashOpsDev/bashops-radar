import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests


GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
PROFILE_CACHE_HOURS = 24
OWNER_REFRESH_HOURS = 6
MAX_PULL_REQUESTS = 50
MAX_ISSUES = 30
MAX_EXTERNAL_REPOSITORIES = 10
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


class DeveloperProfileError(Exception):
    def __init__(self, message: str, code: str = "github_unavailable"):
        super().__init__(message)
        self.message = message
        self.code = code


def normalize_github_username(value: str) -> str:
    username = (value or "").strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise DeveloperProfileError(
            "Enter a valid GitHub username using letters, numbers, or single hyphens.",
            "invalid_username",
        )
    return username.lower()


def _headers() -> dict:
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
            timeout=20,
        )
    except requests.exceptions.Timeout as exc:
        raise DeveloperProfileError("GitHub profile analysis timed out. Please try again.", "github_timeout") from exc
    except requests.exceptions.ConnectionError as exc:
        raise DeveloperProfileError("Could not connect to GitHub. Please try again.", "github_unavailable") from exc
    except requests.exceptions.RequestException as exc:
        raise DeveloperProfileError("GitHub profile analysis is temporarily unavailable.", "github_unavailable") from exc

    if response.status_code == 404:
        raise DeveloperProfileError("That public GitHub user could not be found.", "github_user_not_found")
    if response.status_code in {403, 429}:
        raise DeveloperProfileError("GitHub's public API rate limit was reached. Please try again later.", "github_rate_limit")
    if response.status_code != 200:
        raise DeveloperProfileError("GitHub profile analysis is temporarily unavailable.", "github_unavailable")
    try:
        return response.json()
    except ValueError as exc:
        raise DeveloperProfileError("GitHub returned an unreadable response. Please try again.", "github_unavailable") from exc


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _safe_github_url(value: str, fallback: str = "") -> str:
    try:
        parsed = urlsplit(value or "")
    except ValueError:
        return fallback
    if parsed.scheme == "https" and parsed.hostname and parsed.hostname.casefold() == "github.com":
        return parsed._replace(query="", fragment="").geturl()[:500]
    return fallback


def _safe_avatar_url(value: str) -> str:
    try:
        parsed = urlsplit(value or "")
    except ValueError:
        return ""
    allowed_hosts = {"avatars.githubusercontent.com", "github.com"}
    if parsed.scheme == "https" and parsed.hostname and parsed.hostname.casefold() in allowed_hosts:
        return parsed._replace(query="", fragment="").geturl()[:500]
    return ""


def _repo_full_name(item: dict) -> str:
    repository_url = item.get("repository_url") or ""
    if "/repos/" not in repository_url:
        return ""
    value = repository_url.split("/repos/", 1)[1].strip("/")
    parts = value.split("/")
    if len(parts) != 2 or not all(parts):
        return ""
    return f"{parts[0]}/{parts[1]}"


CATEGORY_RULES = [
    ("Security", ("security", "vulnerability", "cve", "xss", "csrf", "authentication", "authorization")),
    ("Testing", ("test", "tests", "coverage", "flaky", "pytest", "jest", "spec")),
    ("Documentation", ("docs", "documentation", "readme", "guide", "tutorial", "example")),
    ("CI/CD", ("ci", "cd", "workflow", "github actions", "pipeline", "release")),
    ("DevOps", ("docker", "kubernetes", "deploy", "deployment", "terraform", "helm", "observability")),
    ("Backend APIs", ("api", "endpoint", "graphql", "rest", "webhook", "backend", "server", "sdk")),
    ("Frontend", ("frontend", "react", "vue", "svelte", "css", "ui", "accessibility")),
    ("Data", ("database", "sql", "data", "migration", "query", "postgres", "analytics")),
    ("AI/ML", ("ai", "ml", "llm", "model", "inference", "embedding", "vector")),
    ("Infrastructure", ("infrastructure", "distributed", "queue", "cache", "network", "runtime")),
    ("Automation", ("automation", "script", "bot", "generator", "command")),
    ("Refactoring", ("refactor", "cleanup", "simplify", "restructure", "deprecate")),
    ("Bug Fixes", ("bug", "fix", "error", "crash", "regression", "broken")),
    ("Features", ("feature", "add", "implement", "support", "introduce")),
]


def classify_contribution(item: dict, repository: dict) -> tuple[str, list[str]]:
    labels = [
        _text(label.get("name") if isinstance(label, dict) else label, 80)
        for label in item.get("labels", [])
    ]
    topics = [_text(topic, 80) for topic in repository.get("topics", [])[:10]]
    title = _text(item.get("title"), 300)
    body = _text(item.get("body"), 2000)
    language = _text(repository.get("language"), 80)
    searchable = " ".join([title, body, " ".join(labels), " ".join(topics), language]).casefold()

    for category, keywords in CATEGORY_RULES:
        matches = [keyword for keyword in keywords if re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", searchable)]
        if matches:
            evidence = [f"Matched public contribution metadata: {', '.join(matches[:3])}."]
            if language:
                evidence.append(f"Repository language: {language}.")
            return category, evidence

    evidence = ["No specific category keyword was strong enough for a narrower classification."]
    if language:
        evidence.append(f"Repository language: {language}.")
    return "Maintenance", evidence


def _repository_metadata(owned_repositories: list[dict], search_items: list[dict]) -> tuple[dict, list[str]]:
    metadata = {}
    for repository in owned_repositories:
        full_name = _text(repository.get("full_name"), 255)
        if full_name and not repository.get("private"):
            metadata[full_name.casefold()] = repository

    external_names = []
    for item in search_items:
        full_name = _repo_full_name(item)
        if full_name and full_name.casefold() not in metadata and full_name.casefold() not in {
            name.casefold() for name in external_names
        }:
            external_names.append(full_name)

    skipped = []
    for full_name in external_names[:MAX_EXTERNAL_REPOSITORIES]:
        try:
            repository = _github_get(f"/repos/{full_name}")
        except DeveloperProfileError as exc:
            if exc.code == "github_user_not_found":
                skipped.append(full_name)
                continue
            raise
        if repository.get("private"):
            skipped.append(full_name)
            continue
        metadata[full_name.casefold()] = repository

    skipped.extend(external_names[MAX_EXTERNAL_REPOSITORIES:])
    return metadata, skipped


def _contribution_record(item: dict, repository: dict, kind: str) -> dict:
    full_name = _text(repository.get("full_name"), 255)
    category, evidence = classify_contribution(item, repository)
    pull_data = item.get("pull_request") if isinstance(item.get("pull_request"), dict) else {}
    merged_at = pull_data.get("merged_at")
    if kind == "Pull Request" and merged_at:
        status = "Merged"
    else:
        status = "Open" if item.get("state") == "open" else "Closed"

    contribution_url = _safe_github_url(
        item.get("html_url") or "",
        f"https://github.com/{full_name}" if full_name else "https://github.com",
    )
    repository_url = _safe_github_url(
        repository.get("html_url") or "",
        f"https://github.com/{full_name}" if full_name else "https://github.com",
    )
    return {
        "kind": kind,
        "title": _text(item.get("title"), 300) or "Untitled public contribution",
        "url": contribution_url,
        "number": int(item.get("number") or 0),
        "status": status,
        "date": _text(merged_at or item.get("closed_at") or item.get("updated_at") or item.get("created_at"), 40),
        "repository": full_name,
        "repository_url": repository_url,
        "language": _text(repository.get("language"), 80) or "Not specified",
        "repository_stars": int(repository.get("stargazers_count") or 0),
        "category": category,
        "evidence": evidence,
    }


def _strength_data(records: list[dict], username: str) -> dict:
    category_counts = Counter(record["category"] for record in records)
    language_counts = Counter(
        record["language"] for record in records if record.get("language") and record["language"] != "Not specified"
    )
    category_repositories = defaultdict(set)
    category_languages = defaultdict(set)
    category_examples = defaultdict(list)
    for record in records:
        category = record["category"]
        category_repositories[category].add(record["repository"])
        if record["language"] != "Not specified":
            category_languages[category].add(record["language"])
        if len(category_examples[category]) < 3:
            category_examples[category].append(
                {"title": record["title"], "url": record["url"], "repository": record["repository"]}
            )

    total = len(records)
    categories = []
    for category, count in category_counts.most_common(6):
        categories.append(
            {
                "label": category,
                "count": count,
                "percentage": round((count / total) * 100) if total else 0,
                "repositories": sorted(category_repositories[category])[:5],
                "languages": sorted(category_languages[category])[:5],
                "examples": category_examples[category],
            }
        )

    languages = [{"label": label, "count": count} for label, count in language_counts.most_common(6)]
    top_categories = [item["label"] for item in categories[:3]]
    top_languages = [item["label"] for item in languages[:3]]
    repository_count = len({record["repository"] for record in records if record.get("repository")})

    if records:
        category_text = ", ".join(top_categories) if top_categories else "general maintenance"
        language_text = ", ".join(top_languages) if top_languages else "languages not consistently available"
        narrative = (
            f"Across {total} public contribution records found, @{username}'s strongest contribution signals are "
            f"{category_text}. The public repository evidence most often involves {language_text}."
        )
    else:
        narrative = (
            f"No public pull request or issue records were found for @{username} within the bounded GitHub API search. "
            "This does not mean the account has no contribution history."
        )

    return {
        "categories": categories,
        "languages": languages,
        "narrative": narrative,
        "portfolio_summary": "\n".join(
            [
                "Open-source contribution summary",
                "",
                f"- {sum(1 for record in records if record['kind'] == 'Pull Request' and record['status'] == 'Merged')} merged public pull requests found",
                f"- Public contributions found across {repository_count} repositories",
                f"- Strongest contribution signals: {', '.join(top_categories) if top_categories else 'No dominant category found'}",
                f"- Primary languages: {', '.join(top_languages) if top_languages else 'Not enough public language data'}",
                "",
                "Based on public GitHub activity available through the GitHub API.",
                "Generated with BashOps Radar",
            ]
        ),
    }


def analyze_developer_profile(username: str) -> dict:
    normalized_username = normalize_github_username(username)
    public_user = _github_get(f"/users/{normalized_username}")
    if public_user.get("type") != "User":
        raise DeveloperProfileError(
            "BashOps profiles currently support individual public GitHub users, not organizations.",
            "github_organization",
        )
    if not public_user.get("id") or not public_user.get("login"):
        raise DeveloperProfileError(
            "GitHub returned incomplete public profile data. Please try again later.",
            "github_incomplete_profile",
        )

    owned_repositories = _github_get(
        f"/users/{normalized_username}/repos",
        {"type": "owner", "sort": "updated", "direction": "desc", "per_page": 30},
    )
    pull_search = _github_get(
        "/search/issues",
        {
            "q": f"author:{normalized_username} type:pr is:public",
            "sort": "updated",
            "order": "desc",
            "per_page": MAX_PULL_REQUESTS,
        },
    )
    issue_search = _github_get(
        "/search/issues",
        {
            "q": f"author:{normalized_username} type:issue is:public",
            "sort": "updated",
            "order": "desc",
            "per_page": MAX_ISSUES,
        },
    )

    pull_items = pull_search.get("items") if isinstance(pull_search, dict) else []
    issue_items = issue_search.get("items") if isinstance(issue_search, dict) else []
    pull_items = pull_items if isinstance(pull_items, list) else []
    issue_items = issue_items if isinstance(issue_items, list) else []
    repository_metadata, skipped_repositories = _repository_metadata(
        owned_repositories if isinstance(owned_repositories, list) else [],
        pull_items + issue_items,
    )

    records = []
    for item, kind in [(item, "Pull Request") for item in pull_items] + [
        (item, "Public Issue") for item in issue_items
    ]:
        full_name = _repo_full_name(item)
        repository = repository_metadata.get(full_name.casefold()) if full_name else None
        if not repository or repository.get("private"):
            continue
        records.append(_contribution_record(item, repository, kind))
    records.sort(key=lambda record: record.get("date") or "", reverse=True)

    strength_data = _strength_data(records, normalized_username)
    repositories_contributed = len({record["repository"] for record in records if record.get("repository")})
    pull_records = [record for record in records if record["kind"] == "Pull Request"]
    issue_records = [record for record in records if record["kind"] == "Public Issue"]
    partial_reasons = []
    if pull_search.get("incomplete_results") or issue_search.get("incomplete_results"):
        partial_reasons.append("GitHub marked one or more public search result sets as incomplete.")
    if skipped_repositories:
        partial_reasons.append("Some repository metadata was unavailable or outside the bounded repository sample.")
    if len(pull_items) >= MAX_PULL_REQUESTS or len(issue_items) >= MAX_ISSUES:
        partial_reasons.append("The bounded GitHub search reached its per-request result limit.")

    canonical_username = _text(public_user.get("login"), 39) or normalized_username
    profile_url = _safe_github_url(
        public_user.get("html_url") or "",
        f"https://github.com/{canonical_username}",
    )
    return {
        "github_username": canonical_username.lower(),
        "github_user_id": str(public_user.get("id") or ""),
        "display_name": _text(public_user.get("name"), 255) or canonical_username,
        "avatar_url": _safe_avatar_url(public_user.get("avatar_url") or ""),
        "bio": _text(public_user.get("bio"), 500),
        "public_location": _text(public_user.get("location"), 255),
        "profile_url": profile_url,
        "profile_data": {
            "public_contribution_records_analyzed": len(records),
            "public_pull_requests_found": len(pull_records),
            "merged_pull_requests_found": sum(1 for record in pull_records if record["status"] == "Merged"),
            "open_pull_requests_found": sum(1 for record in pull_records if record["status"] == "Open"),
            "public_issues_found": len(issue_records),
            "repositories_contributed_to": repositories_contributed,
            "public_repositories_owned": int(public_user.get("public_repos") or 0),
            "is_partial": bool(partial_reasons),
            "partial_reasons": partial_reasons,
            "api_disclaimer": "Based on public GitHub activity available through the GitHub API.",
        },
        "strength_data": strength_data,
        "contribution_data": records,
    }
