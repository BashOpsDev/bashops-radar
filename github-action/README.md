# BashOps Radar GitHub Action

Find GitHub issues with the highest potential to become paid proof-of-work opportunities.

BashOps Radar analyzes a repository, scores the opportunity, identifies the best first issue, and turns the result into a practical next action for developers, maintainers, freelancers, and agencies.

Analyze your repository free at [bashops.site](https://bashops.site).

## What It Does

- Scores a GitHub repository from 0-100.
- Estimates paid sprint or contract potential.
- Highlights the best issue to start with.
- Suggests a focused proof-of-work next action.
- Optionally comments the summary on a GitHub issue.

## Why It Matters

Open-source work is most valuable when it compounds into trust, founder outreach, and paid opportunities. BashOps Radar helps you focus on repositories and issues where a useful PR can become the start of a paid sprint conversation.

## Quick Start

```yaml
name: BashOps Radar

on:
  workflow_dispatch:

jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - uses: BashOpsDev/bashops-radar/github-action@main
        with:
          repo_url: https://github.com/sourcebot-dev/sourcebot
```

## Outputs

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

      - run: |
          echo "Score: ${{ steps.radar.outputs.opportunity_score }}"
          echo "Potential: ${{ steps.radar.outputs.contract_potential }}"
          echo "Next: ${{ steps.radar.outputs.recommended_next_action }}"
```

## Example Output

```text
BashOps Radar Opportunity Summary
Repository: sourcebot-dev/sourcebot
Opportunity Score: 86/100
Contract Potential: High
Best Issue: #1277 - Improve GitHub integration reliability
Recommended Next Action: Start with #1277 - Improve GitHub integration reliability
Full analysis: https://bashops.site
```

## Comment On An Issue

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

The comment looks like:

```text
BashOps Radar Opportunity Score: 86/100

Contract Potential: High

Recommended Next Action: Start with #1277 - Improve GitHub integration reliability

Full analysis: https://bashops.site
```

## Inputs

| Input | Required | Default | Description |
| --- | --- | --- | --- |
| `repo_url` | Yes |  | GitHub repository URL to analyze. |
| `issue_number` | No |  | Issue number to prioritize in the result. |
| `bashops_api_url` | No | `https://bashops.site/api/v1/analyze` | BashOps Radar API endpoint. |
| `github_token` | No |  | Token used only when `comment=true`. |
| `comment` | No | `false` | Post a summary comment on the issue. |

## Outputs

| Output | Description |
| --- | --- |
| `opportunity_score` | Repository opportunity score from 0-100. |
| `contract_potential` | Estimated paid sprint or contract potential. |
| `recommended_next_action` | Suggested proof-of-work next step. |

## Use Cases

**OSS contributors**  
Pick issues where a useful PR is more likely to get noticed.

**Maintainers**  
Highlight contributor-friendly issues that deserve attention.

**Freelancers**  
Find repositories where proof-of-work can lead to founder outreach and paid sprint potential.

**Agencies**  
Build a repeatable proof-of-work pipeline for technical client acquisition.

## BashOps Radar

BashOps Radar helps developers turn open-source proof-of-work into paid opportunities with repository analysis, opportunity scoring, founder outreach, and a focused pipeline.

Analyze your repository free at [https://bashops.site](https://bashops.site).
