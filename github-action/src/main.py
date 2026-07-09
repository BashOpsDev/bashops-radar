import json
import os
import sys
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def get_input(name: str, default: str = "") -> str:
    return os.getenv(f"INPUT_{name.upper()}", default).strip()


def write_output(name: str, value) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as output_file:
        output_file.write(f"{name}={value}\n")


def request_json(url: str, payload: dict, headers: Optional[dict] = None) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "bashops-radar-github-action",
        "X-BashOps-Client": "github-action",
    }
    if headers:
        request_headers.update(headers)

    request = Request(url, data=body, headers=request_headers, method="POST")
    try:
        with urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"BashOps API returned HTTP {error.code}: {message}") from error
    except URLError as error:
        raise RuntimeError(f"Could not reach BashOps API: {error.reason}") from error
    except TimeoutError as error:
        raise RuntimeError("Timed out while calling BashOps API.") from error


def post_issue_comment(issue_number: str, github_token: str, summary: str) -> None:
    repository = os.getenv("GITHUB_REPOSITORY", "")
    if not repository:
        raise RuntimeError("GITHUB_REPOSITORY is not available, so the issue comment cannot be posted.")

    url = f"https://api.github.com/repos/{repository}/issues/{issue_number}/comments"
    payload = {"body": summary}
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    request_json(url, payload, headers=headers)


def main() -> int:
    repo_url = get_input("repo_url")
    issue_number = get_input("issue_number")
    api_url = get_input("bashops_api_url", "https://bashops.site/api/v1/analyze")
    github_token = get_input("github_token")
    should_comment = get_input("comment", "false").lower() == "true"

    if not repo_url:
        print("BashOps Radar error: repo_url is required.", file=sys.stderr)
        return 1

    payload = {"repo_url": repo_url}
    if issue_number:
        payload["issue_number"] = issue_number

    print("BashOps Radar: analyzing repository opportunity...")
    try:
        result = request_json(api_url, payload)
    except RuntimeError as error:
        print(f"BashOps Radar error: {error}", file=sys.stderr)
        return 1

    if "error" in result:
        print(f"BashOps Radar error: {result['error']}", file=sys.stderr)
        return 1

    score = result.get("opportunity_score", "")
    contract_potential = result.get("contract_potential", "")
    difficulty = result.get("difficulty", "")
    next_action = result.get("recommended_next_action", "")
    repository = result.get("repository", repo_url)
    best_issue = result.get("best_issue") or "No ranked issue returned"

    write_output("opportunity_score", score)
    write_output("contract_potential", contract_potential)
    write_output("difficulty", difficulty)
    write_output("recommended_next_action", next_action)

    print("")
    print("BashOps Radar Opportunity Summary")
    print(f"Repository: {repository}")
    print(f"Opportunity Score: {score}/100")
    print(f"Contract Potential: {contract_potential}")
    if difficulty:
        print(f"Difficulty: {difficulty}")
    print(f"Best Issue: {best_issue}")
    print(f"Recommended Next Action: {next_action}")
    print("Full analysis: https://bashops.site")

    if should_comment:
        if not issue_number or not github_token:
            print("BashOps Radar: comment=true was set, but issue_number or github_token is missing. Skipping comment.")
        else:
            comment = (
                f"**BashOps Radar Opportunity Score:** {score}/100\n\n"
                f"**Contract Potential:** {contract_potential}\n\n"
                f"**Difficulty:** {difficulty or 'Estimate unavailable'}\n\n"
                f"**Recommended Next Action:** {next_action}\n\n"
                "Full analysis: https://bashops.site"
            )
            try:
                post_issue_comment(issue_number, github_token, comment)
                print("BashOps Radar: issue comment posted.")
            except RuntimeError as error:
                print(f"BashOps Radar comment error: {error}", file=sys.stderr)
                return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
