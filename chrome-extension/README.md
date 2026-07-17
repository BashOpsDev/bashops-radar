# BashOps Radar Chrome Extension

This Manifest V3 extension displays cached BashOps Radar opportunity signals on public GitHub repository pages, explains why a repository may be worth contributing to, and links directly to today's opportunity feed.

## Local validation

1. Open `chrome://extensions`.
2. Enable **Developer mode**.
3. Select **Load unpacked** and choose this `chrome-extension` directory.
4. Open a root repository URL such as `https://github.com/Expensify/App`.
5. Open the BashOps Radar extension popup.

The extension never starts a repository analysis. It requests only `GET /api/public/repository-summary`; when no cached summary exists, it links to the full analysis on [bashops.site](https://bashops.site).

## Permissions and privacy

- Host access is limited to `github.com` and `bashops.site`.
- There is no service worker or background tracking.
- The active URL is used only to identify an open repository.
- No browsing history, GitHub token, page content, or repository data is stored by the extension.
