# BashOps Radar Launch Snippets

## LinkedIn Launch Post

I just launched the first public distribution layer for BashOps Radar: a public API and GitHub Action for scoring open-source repositories by Proof-of-Work opportunity potential.

BashOps Radar helps developers find GitHub issues with the highest potential to become paid Proof-of-Work opportunities.

The workflow is simple:

1. Analyze a repository.
2. Get an opportunity score.
3. Identify the best issue to start with.
4. Submit focused proof-of-work.
5. Use that trust to start better founder outreach conversations.

The GitHub Action can now print:

- Opportunity Score
- Contract Potential
- Recommended Next Action

This is the first step toward a broader developer distribution layer: public API, GitHub Action, future CLI, MCP server, VS Code extension, and browser extension.

Try it free: https://bashops.site

GitHub Action: https://github.com/BashOpsDev/bashops-radar/tree/main/github-action

## Short X Post

I built a GitHub Action for BashOps Radar.

It scores GitHub repos and issues by paid Proof-of-Work potential:

- Opportunity Score
- Contract Potential
- Best Issue
- Recommended Next Action

Try it free: https://bashops.site

## Dev.to Article Outline

Title: I Built a GitHub Action That Scores Open-Source Issues for Paid-Work Potential

Sections:

1. The problem: most OSS contribution is unfocused.
2. The opportunity: useful PRs can create trust and paid sprint conversations.
3. How BashOps Radar scores repositories.
4. What the GitHub Action does.
5. Example workflow YAML.
6. Example output from a real repository.
7. How freelancers and agencies can use it.
8. Why the public API matters.
9. What comes next: CLI, MCP, VS Code, browser extension.
10. Try it free at https://bashops.site.

## GitHub Action Announcement Post

BashOps Radar now has a GitHub Action.

Add it to a workflow to score a repository for:

- paid sprint potential
- chance of getting noticed
- best issue to start with
- recommended proof-of-work next action

Example:

```yaml
- id: radar
  uses: BashOpsDev/bashops-radar/github-action@main
  with:
    repo_url: https://github.com/sourcebot-dev/sourcebot
```

The Action is powered by the same public API used by the BashOps Radar web app.

Try it: https://bashops.site

## First Tutorial Outline

Title: I Built a GitHub Action That Scores Open-Source Issues for Paid-Work Potential

1. Start with the problem: not every GitHub issue is worth your time.
2. Explain the Proof-of-Work to paid sprint workflow.
3. Show the BashOps Radar API response.
4. Add the GitHub Action workflow.
5. Run the Action manually.
6. Read the output: score, contract potential, next action.
7. Enable issue comment mode.
8. Discuss safe usage: do useful work first, pitch later.
9. Link to the web app and Action README.

## 10 Post Ideas For The Next Week

1. How I decide whether a GitHub issue is worth contributing to.
2. Why Proof-of-Work beats cold outreach for developer services.
3. A 5-minute workflow for finding better OSS contribution targets.
4. What makes an open-source repository a good paid sprint lead.
5. How to use BashOps Radar inside GitHub Actions.
6. Why "good first issue" is not always the best first issue.
7. Turning a useful PR into founder outreach without sounding salesy.
8. The difference between contribution potential and contract potential.
9. How I would use BashOps Radar as a freelancer.
10. The roadmap from web app to API, GitHub Action, CLI, MCP, and IDE tools.
