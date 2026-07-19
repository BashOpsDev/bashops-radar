from radar import (
    decision,
    estimate_difficulty as estimate_issue_difficulty,
    estimate_time as estimate_issue_time,
    get_analysis,
    merge_probability as estimate_issue_merge_probability,
    recommendation,
    recommend_angle,
    score_repo_signal_report,
)
from repository_intelligence import build_repository_intelligence


def contract_potential(score: int) -> str:
    if score >= 85:
        return "High"
    if score >= 60:
        return "Medium"
    return "Low"


def _select_issue(issue_rankings, issue_number=None):
    if not issue_rankings:
        return None

    if issue_number is not None:
        try:
            requested_number = int(issue_number)
        except (TypeError, ValueError):
            requested_number = None

        if requested_number is not None:
            for score, issue_type, issue in issue_rankings:
                if issue.get("number") == requested_number:
                    return score, issue_type, issue

    return issue_rankings[0]


def build_analysis_result(repo_url: str, issue_number=None) -> dict:
    owner, repo_name, repo, languages, issue_rankings, repo_score, language_badge = get_analysis(repo_url)

    selected_issue = _select_issue(issue_rankings, issue_number)
    best_issue = None
    difficulty = "Unavailable"
    estimated_time = "Unavailable"
    merge_probability = "Unavailable"
    if selected_issue:
        score, issue_type, issue = selected_issue
        best_issue = {
            "number": issue.get("number"),
            "title": issue.get("title"),
            "url": issue.get("html_url"),
            "score": score,
            "type": issue_type,
        }
        difficulty = estimate_issue_difficulty(issue_type, score)
        estimated_time = estimate_issue_time(issue_type)
        merge_probability = estimate_issue_merge_probability(score, repo_score)

    angle = recommend_angle(languages)
    score_transparency = score_repo_signal_report(
        repo,
        [issue for _score, _issue_type, issue in issue_rankings],
        languages,
    )
    # Keep the displayed score and its component explanation on one source of
    # truth even when callers provide precomputed analysis data.
    repo_score = score_transparency["score"]
    if selected_issue:
        merge_probability = estimate_issue_merge_probability(selected_issue[0], repo_score)
    repository_intelligence = build_repository_intelligence(
        repo,
        [issue for _score, _issue_type, issue in issue_rankings],
    )
    recommendation_result = recommendation(
        repo_score,
        best_issue=best_issue,
        confidence=score_transparency["confidence"],
    )
    if recommendation_result["decision"] == "Contribute Now":
        recommended_action = f"Start with #{best_issue['number']} - {best_issue['title']}"
    elif best_issue and recommendation_result["decision"] == "Inspect Carefully":
        recommended_action = f"Review the scope of #{best_issue['number']} before starting work"
    elif recommendation_result["decision"] == "Inspect Repository":
        recommended_action = "Inspect the repository issue queue before choosing work"
    else:
        recommended_action = "Prioritize a repository with stronger public evidence"

    outcome_by_recommendation = {
        "Contribute Now": "Use one focused contribution to test maintainer fit before considering outreach.",
        "Inspect Carefully": "Verify issue scope and maintainer activity before deciding whether to contribute.",
        "Inspect Repository": "Find a current, scoped issue before deciding whether to contribute.",
        "Skip": "Spend time on a repository with stronger and more complete public evidence.",
    }

    return {
        "repo": f"{owner}/{repo_name}",
        "repo_url": repo_url,
        "owner": owner,
        "repo_name": repo_name,
        "repo_data": repo,
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
            "Exceptional"
            if repo_score >= 98
            else "Excellent"
            if repo_score >= 90
            else "Strong"
            if repo_score >= 80
            else "Moderate"
            if repo_score >= 60
            else "Weak"
        ),
        "score_action": recommendation_result["label"],
        "recommendation_explanation": recommendation_result["explanation"],
        "contract_potential": contract_potential(repo_score),
        "merge_probability": merge_probability,
        "estimated_time": estimated_time,
        "difficulty": difficulty,
        "decision": decision(
            repo_score,
            best_issue=best_issue,
            confidence=score_transparency["confidence"],
        ),
        "angle": angle,
        "best_issue": best_issue,
        "recommended_action": recommended_action,
        "recommended_outcome": outcome_by_recommendation[recommendation_result["decision"]],
        "issues": issue_rankings[:8],
        "languages": languages,
        "score_transparency": score_transparency,
        "repository_intelligence": repository_intelligence,
    }


def to_public_api_payload(result: dict, site_url: str) -> dict:
    best_issue = result.get("best_issue")
    best_issue_text = None
    if best_issue:
        best_issue_text = f"#{best_issue.get('number')} - {best_issue.get('title')}"

    return {
        "repository": result.get("repo", ""),
        "opportunity_score": result.get("score", 0),
        "decision": result.get("decision", ""),
        "chance_of_getting_noticed": "Unavailable - maintainer attention is not measured",
        "contract_potential": contract_potential(int(result.get("score") or 0)),
        "merge_probability": result.get("merge_probability", ""),
        "estimated_time": result.get("estimated_time", ""),
        "difficulty": result.get("difficulty", ""),
        "best_issue": best_issue_text,
        "best_issue_url": best_issue.get("url") if best_issue else None,
        "proof_of_work_angle": result.get("angle", ""),
        "recommended_next_action": result.get("recommended_action", ""),
        "upgrade_url": f"{site_url.rstrip('/')}/pricing",
    }
