from radar import (
    days_since,
    decision,
    estimate_difficulty as estimate_issue_difficulty,
    estimate_time as estimate_issue_time,
    get_analysis,
    merge_probability as estimate_issue_merge_probability,
    recommend_angle,
)


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


def build_score_transparency(repo, languages, issue_rankings) -> dict:
    """Explain the existing score inputs without exposing internal weights."""
    open_issues = int(repo.get("open_issues_count") or 0)
    stars = int(repo.get("stargazers_count") or 0)
    forks = int(repo.get("forks_count") or 0)
    last_push_days = days_since(repo.get("pushed_at"))
    issue_scores = [score for score, _issue_type, _issue in issue_rankings]
    average_issue_score = sum(issue_scores) / len(issue_scores) if issue_scores else 0
    target_languages = [lang for lang in ("Python", "TypeScript", "JavaScript") if lang in languages]
    recent_issue_count = sum(1 for _score, _type, issue in issue_rankings if days_since(issue.get("updated_at")) <= 30)
    discussed_issue_count = sum(1 for _score, _type, issue in issue_rankings if int(issue.get("comments") or 0) > 0)

    reasons = []
    warnings = []

    if last_push_days <= 3:
        reasons.append(_quality_label("Recently maintained", f"Last push: {_days_label(last_push_days)}"))
    elif last_push_days <= 14:
        reasons.append(_quality_label("Active maintenance", f"Last push: {_days_label(last_push_days)}"))
    elif last_push_days <= 30:
        reasons.append(_quality_label("Maintenance signal available", f"Last push: {_days_label(last_push_days)}"))
    else:
        warnings.append(_quality_label("Maintenance recency is limited", f"Last push: {_days_label(last_push_days)}"))

    if 5 <= open_issues <= 80:
        reasons.append(_quality_label("Healthy issue backlog", f"{open_issues} open issues"))
    elif open_issues > 80:
        reasons.append(_quality_label("Large issue backlog", f"{open_issues} open issues"))
        warnings.append(_quality_label("Issue volume may require filtering", "Large backlogs can contain stale or noisy work"))
    elif open_issues > 0:
        reasons.append(_quality_label("Some issue activity", f"{open_issues} open issues"))
    else:
        warnings.append(_quality_label("No open issue signal", "GitHub reports no open issues"))

    if 20 <= stars <= 5000:
        reasons.append(_quality_label("Healthy repository visibility", f"{stars} stars"))
    elif stars > 5000:
        reasons.append(_quality_label("Strong ecosystem visibility", f"{stars} stars"))
        warnings.append(_quality_label("Popular repository", "Higher visibility can mean more contributor competition"))
    elif stars > 0:
        reasons.append(_quality_label("Early visibility signal", f"{stars} stars"))

    if 2 <= forks <= 500:
        reasons.append(_quality_label("Community activity present", f"{forks} forks"))
    elif forks > 500:
        reasons.append(_quality_label("Broad ecosystem activity", f"{forks} forks"))
        warnings.append(_quality_label("Large contributor surface", "Many forks can indicate a crowded project"))

    if languages:
        reasons.append(_quality_label("Language profile available", ", ".join(list(languages.keys())[:3])))
    else:
        warnings.append(_quality_label("Language signal unavailable", "GitHub did not return language data"))

    if target_languages:
        reasons.append(_quality_label("Good contributor fit", ", ".join(target_languages) + " detected"))

    if average_issue_score >= 75:
        reasons.append(_quality_label("Strong issue quality", "Recent, active issues found"))
    elif average_issue_score >= 60:
        reasons.append(_quality_label("Usable issue quality", "Some issues look suitable for focused work"))
    elif issue_rankings:
        warnings.append(_quality_label("Issue quality needs review", "Inspect the top issues before committing time"))
    else:
        warnings.append(_quality_label("No ranked issues found", "Manual inspection is required"))

    if discussed_issue_count:
        reasons.append(_quality_label("Active issue discussion", f"{discussed_issue_count} ranked issues have comments"))

    signals_used = [
        _quality_label(
            "Repository Activity",
            "Excellent" if last_push_days <= 3 else "Strong" if last_push_days <= 14 else "Moderate" if last_push_days <= 30 else "Limited",
        ),
        _quality_label(
            "Issue Quality",
            "Excellent" if average_issue_score >= 80 else "Strong" if average_issue_score >= 70 else "Moderate" if average_issue_score >= 60 else "Limited",
        ),
        _quality_label(
            "Community Health",
            "Strong" if forks >= 2 and open_issues > 0 else "Moderate" if stars > 0 or open_issues > 0 else "Limited",
        ),
        _quality_label(
            "Competition",
            "High" if stars > 5000 or forks > 500 else "Medium" if stars >= 1000 or forks >= 100 else "Low",
        ),
        _quality_label(
            "Maintainer Activity",
            "High" if last_push_days <= 14 and (recent_issue_count or discussed_issue_count) else "Medium" if last_push_days <= 30 else "Low",
        ),
    ]

    confidence_reasons = []
    if last_push_days <= 30:
        confidence_reasons.append("Recent repository activity")
    if discussed_issue_count:
        confidence_reasons.append("Active issue discussion")
    if recent_issue_count:
        confidence_reasons.append("Recent issue updates")
    if repo.get("pushed_at") and repo.get("open_issues_count") is not None and languages:
        confidence_reasons.append("Repository metadata available")
    if issue_rankings:
        confidence_reasons.append("Ranked issue candidates found")

    confidence = "High" if len(confidence_reasons) >= 4 else "Medium" if len(confidence_reasons) >= 2 else "Low"

    return {
        "reasons": reasons[:7],
        "warnings": warnings[:3],
        "signals_used": signals_used,
        "confidence": confidence,
        "confidence_reasons": confidence_reasons[:5],
    }


def build_analysis_result(repo_url: str, issue_number=None) -> dict:
    owner, repo_name, repo, languages, issue_rankings, repo_score, language_badge = get_analysis(repo_url)

    selected_issue = _select_issue(issue_rankings, issue_number)
    best_issue = None
    difficulty = "Medium"
    estimated_time = "2-4 hours" if repo_score >= 85 else "4-8 hours"
    merge_probability = contract_potential(repo_score)
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

    recommended_action = "Analyze another repository"
    if best_issue:
        recommended_action = f"Start with #{best_issue['number']} - {best_issue['title']}"

    angle = recommend_angle(languages)
    score_transparency = build_score_transparency(repo, languages, issue_rankings)

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
            "Excellent"
            if repo_score >= 90
            else "Strong"
            if repo_score >= 80
            else "Moderate"
            if repo_score >= 60
            else "Weak"
        ),
        "score_action": (
            "CONTRIBUTE NOW"
            if repo_score >= 85
            else "INSPECT MANUALLY"
            if repo_score >= 60
            else "SKIP FOR NOW"
        ),
        "merge_probability": merge_probability,
        "estimated_time": estimated_time,
        "difficulty": difficulty,
        "decision": decision(repo_score),
        "angle": angle,
        "best_issue": best_issue,
        "recommended_action": recommended_action,
        "recommended_outcome": "Submit one focused PR, build trust, then pitch a 48-hour sprint.",
        "issues": issue_rankings[:8],
        "languages": languages,
        "score_transparency": score_transparency,
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
        "chance_of_getting_noticed": f"{result.get('score', 0)}%",
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
