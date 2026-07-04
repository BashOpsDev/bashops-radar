# BashOps Radar GitHub Action

Find GitHub issues with the highest potential to become paid Proof-of-Work opportunities.

BashOps Radar analyzes a GitHub repository, scores its opportunity potential, identifies the best issue to start with, and returns a practical next action for turning useful open-source work into founder outreach, paid sprint potential, and a repeatable proof-of-work pipeline.

Analyze a repository free at [https://bashops.site](https://bashops.site).

## What The Action Does

- Scores a GitHub repository from 0-100.
- Estimates contract potential: Low, Medium, or High.
- Finds the highest-signal issue to start with.
- Suggests the next proof-of-work action.
- Optionally posts a summary comment on a GitHub issue.

## Who It Is For

- OSS contributors deciding which issue is worth their time.
- Maintainers surfacing contributor-friendly work.
- Freelancers looking for proof-of-work paths into paid developer contracts.
- Agencies building a repeatable open-source client acquisition workflow.

## Why It Matters

Most open-source contribution is unfocused. BashOps Radar helps you target repositories and issues where a focused PR can build trust, create founder outreach context, and become the start of a paid sprint conversation.

## Quick Start: Manual Scan

Copy this workflow into `.github/workflows/bashops-radar.yml`:

```yaml
name: BashOps Radar

on:
  workflow_dispatch:

jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - id: radar
        uses: BashOpsDev/bashops-radar/github-action@main
        with:
          repo_url: https://github.com/sourcebot-dev/sourcebot

      - name: Show BashOps outputs
        run: |
          echo "Opportunity Score: ${{ steps.radar.outputs.opportunity_score }}"
          echo "Contract Potential: ${{ steps.radar.outputs.contract_potential }}"
          echo "Recommended Next Action: ${{ steps.radar.outputs.recommended_next_action }}"
```

## Weekly Scheduled Scan

Use this when you want a lightweight weekly signal on a repository you care about:

```yaml
name: Weekly BashOps Radar

on:
  schedule:
    - cron: "0 14 * * 1"
  workflow_dispatch:

jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - id: radar
        uses: BashOpsDev/bashops-radar/github-action@main
        with:
          repo_url: https://github.com/sourcebot-dev/sourcebot

      - name: Print opportunity summary
        run: |
          echo "Opportunity Score: ${{ steps.radar.outputs.opportunity_score }}"
          echo "Contract Potential: ${{ steps.radar.outputs.contract_potential }}"
          echo "Recommended Next Action: ${{ steps.radar.outputs.recommended_next_action }}"
```

## Issue Comment Mode

Use this when you want BashOps Radar to comment directly on an issue with a concise opportunity summary.

```yaml
name: BashOps Radar Issue Comment

on:
  workflow_dispatch:
    inputs:
      issue_number:
        description: Issue number to analyze
        required: true

jobs:
  analyze:
    runs-on: ubuntu-latest
    permissions:
      issues: write
    steps:
      - uses: BashOpsDev/bashops-radar/github-action@main
        with:
          repo_url: https://github.com/${{ github.repository }}
          issue_number: ${{ inputs.issue_number }}
          comment: true
          github_token: ${{ secrets.GITHUB_TOKEN }}
```

Example comment:

```text
BashOps Radar Opportunity Score: 86/100

Contract Potential: High

Recommended Next Action: Start with #1277 - Improve GitHub integration reliability

Full analysis: https://bashops.site
```

## Example Action Output

```text
BashOps Radar Opportunity Summary
Repository: sourcebot-dev/sourcebot
Opportunity Score: 86/100
Contract Potential: High
Best Issue: #1277 - Improve GitHub integration reliability
Recommended Next Action: Start with #1277 - Improve GitHub integration reliability
Full analysis: https://bashops.site
```

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `repo_url` | Yes |  | GitHub repository URL to analyze. |
| `issue_number` | No |  | Optional issue number to prioritize in the result. |
| `bashops_api_url` | No | `https://bashops.site/api/v1/analyze` | BashOps Radar API endpoint. |
| `github_token` | No |  | Token used only when `comment=true`. |
| `comment` | No | `false` | Post a summary comment on the issue. |

## Outputs

| Output | Description |
| --- | --- |
| `opportunity_score` | Repository opportunity score from 0-100. |
| `contract_potential` | Estimated paid sprint or contract potential. |
| `recommended_next_action` | Suggested proof-of-work next step. |

## Public API

The Action is a thin client for the BashOps Radar public API:

```text
POST https://bashops.site/api/v1/analyze
```

This keeps the Action simple and makes the same analysis layer reusable later for the CLI, MCP server, VS Code extension, browser extension, and public integrations.

## Learn More

- Website: [https://bashops.site](https://bashops.site)
- API endpoint: `https://bashops.site/api/v1/analyze`
- GitHub Action folder: `github-action/`

Start free: [Analyze your repository on BashOps Radar](https://bashops.site).
