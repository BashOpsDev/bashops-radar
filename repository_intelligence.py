from collections import defaultdict
from datetime import datetime, timezone


STALE_ISSUE_DAYS = 30


def _parse_github_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age_days(value, reference_time=None):
    parsed = _parse_github_datetime(value)
    if not parsed:
        return None
    now = reference_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0, (now.astimezone(timezone.utc) - parsed).days)


def _within_days(value, days, reference_time=None):
    age = _age_days(value, reference_time)
    return age is not None and age <= days


def _labels(issue):
    return {
        str(label.get("name") or "").strip().casefold()
        for label in issue.get("labels", [])
        if str(label.get("name") or "").strip()
    }


def _signal(key, label, value, detail, available=True):
    return {
        "key": key,
        "label": label,
        "value": value,
        "detail": detail,
        "available": available,
    }


def build_repository_intelligence(repo, issues, pull_sample=None, reference_time=None):
    """Build explainable repository signals from data already fetched from GitHub."""
    issues = list(issues or [])
    stars = int(repo.get("stargazers_count") or repo.get("stars") or 0)
    forks = int(repo.get("forks_count") or repo.get("forks") or 0)
    last_push_days = _age_days(repo.get("pushed_at"), reference_time)
    recent_issues = sum(_within_days(issue.get("updated_at"), 30, reference_time) for issue in issues)
    discussed_issues = sum(int(issue.get("comments") or 0) > 0 for issue in issues)
    contributor_labels = sum(
        bool(_labels(issue) & {"good first issue", "help wanted"}) for issue in issues
    )

    if last_push_days is None:
        health_value = "Limited data"
        health_detail = "GitHub did not provide a repository push timestamp."
    elif last_push_days <= 30 and issues:
        health_value = "Strong"
        health_detail = f"Last push was {last_push_days} days ago and {len(issues)} open issues were sampled."
    elif last_push_days <= 90:
        health_value = "Moderate"
        health_detail = f"Last push was {last_push_days} days ago; recent maintenance should be confirmed."
    else:
        health_value = "At risk"
        health_detail = f"Last push was {last_push_days} days ago."

    activity_points = recent_issues + discussed_issues + min(forks, 10)
    activity_value = "High" if activity_points >= 12 else "Moderate" if activity_points >= 4 else "Low"
    activity_detail = (
        f"{recent_issues} sampled issues changed in 30 days, "
        f"{discussed_issues} have discussion, and GitHub reports {forks} forks."
    )

    if last_push_days is None:
        momentum_value = "Unavailable"
        momentum_detail = "A push timestamp was not available."
        momentum_available = False
    else:
        momentum_value = "Growing" if last_push_days <= 14 and recent_issues else "Steady" if last_push_days <= 60 else "Slowing"
        momentum_detail = f"Last push: {last_push_days} days ago; {recent_issues} sampled issues changed in 30 days."
        momentum_available = True

    documentation_evidence = []
    if repo.get("description"):
        documentation_evidence.append("repository description")
    if repo.get("homepage"):
        documentation_evidence.append("project homepage")
    if repo.get("has_wiki"):
        documentation_evidence.append("wiki enabled")
    if repo.get("license"):
        documentation_evidence.append("license metadata")
    docs_value = "Strong signals" if len(documentation_evidence) >= 3 else "Basic signals" if documentation_evidence else "Limited"
    docs_detail = (
        "Available metadata: " + ", ".join(documentation_evidence) + ". README quality was not inspected."
        if documentation_evidence
        else "No documentation metadata was detected; README quality was not inspected."
    )

    owner = repo.get("owner") or {}
    commercial_evidence = []
    if str(owner.get("type") or "").casefold() == "organization":
        commercial_evidence.append("organization-owned")
    if repo.get("homepage"):
        commercial_evidence.append("project homepage")
    if repo.get("has_sponsors"):
        commercial_evidence.append("GitHub Sponsors enabled")
    commercial_value = "Signals present" if commercial_evidence else "Not detected"
    commercial_detail = (
        ", ".join(commercial_evidence) + ". These signals do not prove commercial intent."
        if commercial_evidence
        else "Repository metadata alone does not show a commercial signal."
    )

    pull_sample = pull_sample or {}
    closed_pulls = list(pull_sample.get("closed") or [])
    if pull_sample.get("available") and closed_pulls:
        merged = sum(bool(pull.get("merged_at")) for pull in closed_pulls)
        acceptance = round((merged / len(closed_pulls)) * 100)
        acceptance_value = f"{acceptance}%"
        acceptance_detail = f"{merged} of {len(closed_pulls)} sampled recently closed pull requests were merged."
        acceptance_available = True
    else:
        acceptance_value = "Unavailable"
        acceptance_detail = "Closed pull-request history was not available for this analysis."
        acceptance_available = False

    risk_reasons = []
    if repo.get("archived"):
        risk_reasons.append("repository is archived")
    if last_push_days is not None and last_push_days > 90:
        risk_reasons.append("maintenance activity is stale")
    if int(repo.get("open_issues_count") or repo.get("open_issues") or 0) > 100:
        risk_reasons.append("the open-item backlog is large")
    if not issues:
        risk_reasons.append("no open issue sample was available")
    risk_value = "High" if len(risk_reasons) >= 2 else "Medium" if risk_reasons else "Low"
    risk_detail = "; ".join(risk_reasons).capitalize() + "." if risk_reasons else "No major metadata risk signal was detected."

    if contributor_labels:
        friendliness_value = "Strong"
        friendliness_detail = f"{contributor_labels} sampled issues use good-first-issue or help-wanted labels."
    elif issues:
        friendliness_value = "Moderate"
        friendliness_detail = "Open issues are available, but no explicit contributor-friendly labels were found in the sample."
    else:
        friendliness_value = "Unavailable"
        friendliness_detail = "No issue sample was available."

    return [
        _signal("health", "Repository Health", health_value, health_detail),
        _signal("community_activity", "Community Activity", activity_value, activity_detail),
        _signal(
            "maintainer_responsiveness",
            "Maintainer Responsiveness",
            "Unavailable",
            "Comment authors and first-response timestamps were not requested, so maintainer response speed cannot be verified.",
            False,
        ),
        _signal("momentum", "Repository Momentum", momentum_value, momentum_detail, momentum_available),
        _signal("documentation", "Documentation Quality", docs_value, docs_detail),
        _signal("commercial", "Commercial Signals", commercial_value, commercial_detail),
        _signal("acceptance", "Contributor Acceptance Rate", acceptance_value, acceptance_detail, acceptance_available),
        _signal("risk", "Repository Risk", risk_value, risk_detail),
        _signal("friendliness", "Repository Friendliness", friendliness_value, friendliness_detail),
    ]


def _priority_action(issue):
    number = int(issue.get("number") or 0)
    missing = list(issue.get("missing_information") or [])
    duplicates = list(issue.get("possible_duplicates") or [])
    category = issue.get("suggested_category") or "Other"
    priority = issue.get("estimated_priority") or "Medium"
    suitability = issue.get("contributor_suitability") or "Not enough information"

    if category == "Security":
        action = f"Review security-sensitive issue #{number} privately"
        reason = "Security-related reports require controlled maintainer review before public action."
        minutes = 20
        rank = 100
    elif missing:
        action = f"Request missing information on #{number}"
        reason = "; ".join(missing[:2])
        minutes = 5
        rank = 80 if priority == "High" else 65
    elif duplicates:
        action = f"Compare possible duplicate candidates for #{number}"
        reason = "Similar issue titles were detected; maintainer confirmation is required."
        minutes = 12
        rank = 75
    elif priority == "High":
        action = f"Review high-priority issue #{number}"
        reason = "The issue was classified as estimated high priority from its current content."
        minutes = 10
        rank = 70
    elif suitability in {"Good first contribution", "Suitable for experienced contributor"}:
        action = f"Confirm contributor scope for #{number}"
        reason = f"Current signals classify it as: {suitability}."
        minutes = 8
        rank = 55
    else:
        action = f"Review issue #{number}"
        reason = "The issue needs a maintainer decision before the next workflow step."
        minutes = 8
        rank = 40
    return {"action": action, "reason": reason, "estimated_review_minutes": minutes, "rank": rank}


def build_maintainer_operations(repository, source_issues, report_issues, pull_sample=None, reference_time=None):
    source_issues = list(source_issues or [])
    report_issues = [dict(item) for item in (report_issues or [])]
    pull_sample = pull_sample or {}
    pull_data_available = bool(pull_sample.get("available"))
    open_pulls = list(pull_sample.get("open") or [])
    closed_pulls = list(pull_sample.get("closed") or [])

    stale_issues = sum(
        (_age_days(issue.get("updated_at"), reference_time) or 0) >= STALE_ISSUE_DAYS
        for issue in source_issues
    )
    unanswered_issues = sum(int(issue.get("comments") or 0) == 0 for issue in source_issues)
    review_queue = [pull for pull in open_pulls if not pull.get("draft")]
    oldest_pull = None
    if review_queue:
        oldest = min(review_queue, key=lambda pull: _parse_github_datetime(pull.get("created_at")) or datetime.max.replace(tzinfo=timezone.utc))
        oldest_pull = {
            "number": int(oldest.get("number") or 0),
            "title": str(oldest.get("title") or "Untitled pull request")[:300],
            "url": str(oldest.get("html_url") or ""),
            "waiting_days": _age_days(oldest.get("created_at"), reference_time) or 0,
        }

    priorities = sorted((_priority_action(issue) for issue in report_issues), key=lambda item: item["rank"], reverse=True)[:3]
    for item in priorities:
        item.pop("rank", None)

    def recent(items, field):
        return sum(_within_days(item.get(field), 7, reference_time) for item in items)

    weekly = {
        "issues_opened": recent(source_issues, "created_at"),
        "issues_updated": recent(source_issues, "updated_at"),
        "pull_requests_updated": recent(open_pulls, "updated_at") if pull_data_available else None,
        "pull_requests_merged": recent([pull for pull in closed_pulls if pull.get("merged_at")], "merged_at") if pull_data_available else None,
        "sample_note": "Counts describe the bounded items reviewed by this report, not complete repository history.",
    }

    submissions = {
        "high_value_contribution": sum(
            issue.get("contributor_suitability") in {"Good first contribution", "Suitable for experienced contributor"}
            and issue.get("estimated_priority") in {"High", "Medium"}
            and not issue.get("missing_information")
            for issue in report_issues
        ),
        "needs_more_information": sum(bool(issue.get("missing_information")) for issue in report_issues),
        "likely_duplicate": sum(bool(issue.get("possible_duplicates")) for issue in report_issues),
        "needs_human_review": sum(
            issue.get("estimated_priority") == "Needs Manual Review"
            or issue.get("contributor_suitability") in {"Needs clarification first", "Not enough information"}
            for issue in report_issues
        ),
        "security_sensitive": sum(issue.get("suggested_category") == "Security" for issue in report_issues),
    }

    contributor_stats = defaultdict(lambda: {"open": 0, "closed": 0, "merged": 0, "titles": []})
    for state, pulls in (("open", open_pulls), ("closed", closed_pulls)):
        for pull in pulls:
            author = str((pull.get("user") or {}).get("login") or "").strip()
            if not author:
                continue
            contributor_stats[author][state] += 1
            contributor_stats[author]["merged"] += int(bool(pull.get("merged_at")))
            contributor_stats[author]["titles"].append(str(pull.get("title") or "")[:160])

    contributors = []
    for author, stats in sorted(
        contributor_stats.items(),
        key=lambda item: (item[1]["merged"], item[1]["open"] + item[1]["closed"]),
        reverse=True,
    )[:5]:
        total = stats["open"] + stats["closed"]
        documentation_submissions = sum(
            any(term in title.casefold() for term in ("docs", "documentation", "readme"))
            for title in stats["titles"]
        )
        familiarity = "Strong sample" if total >= 3 else "Emerging sample" if total >= 2 else "Limited sample"
        contributors.append(
            {
                "author": author,
                "sampled_submissions": total,
                "sampled_merged": stats["merged"],
                "repository_familiarity": familiarity,
                "evidence": f"{stats['merged']} merged of {stats['closed']} sampled closed PRs; {stats['open']} currently open.",
                "signals": [
                    {
                        "label": "Merged PR History",
                        "value": f"{stats['merged']} sampled merges",
                        "detail": f"Based on {stats['closed']} sampled closed pull requests.",
                    },
                    {
                        "label": "Repository Familiarity",
                        "value": familiarity,
                        "detail": f"The contributor appears in {total} sampled open or closed pull requests.",
                    },
                    {
                        "label": "Documentation Quality",
                        "value": "Relevant history" if documentation_submissions else "Not observed",
                        "detail": (
                            f"{documentation_submissions} sampled PR titles reference documentation work."
                            if documentation_submissions
                            else "No sampled PR title references documentation; content quality was not inferred."
                        ),
                    },
                    {
                        "label": "Consistency",
                        "value": "Repeat activity" if total >= 2 else "Limited sample",
                        "detail": f"Observed across {total} sampled submissions; this is not complete contributor history.",
                    },
                ],
            }
        )

    paid_candidate = next(
        (item for item in contributors if item["sampled_merged"] >= 2 and item["sampled_submissions"] >= 3),
        None,
    )
    next_issue = next(
        (
            {"number": issue.get("number"), "title": issue.get("title"), "reason": issue.get("contributor_suitability")}
            for issue in report_issues
            if issue.get("contributor_suitability") in {"Good first contribution", "Suitable for experienced contributor"}
            and not issue.get("missing_information")
        ),
        None,
    )

    estimated_minutes = len(source_issues) * 4 + len(review_queue) * 3
    return {
        "workload": {
            "open_issues_reviewed": len(source_issues),
            "github_open_items": int(repository.get("open_issues") or 0),
            "stale_issues": stale_issues,
            "prs_awaiting_review": len(review_queue) if pull_data_available else None,
            "prs_with_failing_checks": None,
            "unanswered_issues": unanswered_issues,
            "oldest_waiting_pr": oldest_pull,
            "average_response_time": None,
            "limitations": [
                "GitHub's repository open-item count can include both issues and pull requests.",
                "Failing checks require per-PR check requests and were not fetched.",
                "Average response time requires comment timelines and was not inferred from update timestamps.",
            ],
        },
        "daily_priorities": priorities,
        "weekly_overview": weekly,
        "submission_intelligence": submissions,
        "estimated_hours_saved": {
            "hours": round(estimated_minutes / 60, 1),
            "detail": f"Estimate uses 4 minutes per reviewed issue and 3 minutes per sampled review-ready PR ({estimated_minutes} minutes total).",
        },
        "contributor_trust": {
            "contributors": contributors,
            "unavailable_signals": [
                "Test quality was not assessed because PR checks and diffs were not fetched.",
                "Review responsiveness was not assessed because review timelines were not fetched.",
                "Scope discipline and reverted work require commit-level history and were not inferred.",
            ],
        },
        "integration": {
            "contributor_history": f"{len(contributors)} contributors appear in the bounded PR sample." if pull_data_available else "PR history unavailable.",
            "relevant_previous_work": contributors[0]["evidence"] if contributors else "No repeat-contributor evidence in the sample.",
            "potential_paid_sprint_candidate": (
                f"@{paid_candidate['author']} has {paid_candidate['sampled_merged']} merged PRs in the sample; human review is required."
                if paid_candidate
                else "No candidate met the transparent sample threshold of two merged PRs and three submissions."
            ),
            "suggested_next_issue": next_issue,
        },
    }
