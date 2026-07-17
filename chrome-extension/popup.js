(function () {
  "use strict";

  const reservedOwners = new Set([
    "collections", "codespaces", "events", "explore", "features", "login", "marketplace",
    "new", "notifications", "organizations", "orgs", "search", "settings", "signup",
    "sponsors", "topics", "trending", "users"
  ]);

  function show(id) {
    document.querySelectorAll("main > section").forEach(function (section) { section.hidden = true; });
    document.getElementById(id).hidden = false;
  }

  function repositoryFromUrl(rawUrl) {
    try {
      const url = new URL(rawUrl);
      const parts = url.pathname.split("/").filter(Boolean);
      if (url.hostname !== "github.com" || parts.length !== 2 || reservedOwners.has(parts[0].toLowerCase())) return null;
      if (!/^[A-Za-z0-9_.-]+$/.test(parts[0]) || !/^[A-Za-z0-9_.-]+$/.test(parts[1])) return null;
      return {owner: parts[0], repo: parts[1]};
    } catch (_error) {
      return null;
    }
  }

  function fallbackAnalyzeUrl(repository) {
    const repoUrl = "https://github.com/" + repository.owner + "/" + repository.repo;
    return "https://bashops.site/?source=extension-analyze&repo_url=" + encodeURIComponent(repoUrl);
  }

  function renderUnavailable(repository, payload) {
    document.getElementById("unavailableRepo").textContent = repository.owner + "/" + repository.repo;
    document.getElementById("analyzeUnavailable").href = payload && payload.analyze_url ? payload.analyze_url : fallbackAnalyzeUrl(repository);
    show("unavailable");
  }

  function renderSummary(payload) {
    document.getElementById("repository").textContent = payload.repository;
    document.getElementById("score").textContent = payload.radar_score + "/100";
    document.getElementById("decision").textContent = payload.decision;
    document.getElementById("difficulty").textContent = payload.difficulty;
    document.getElementById("mergeProbability").textContent = payload.merge_probability;
    document.getElementById("analyze").href = payload.analyze_url;
    document.getElementById("openRadar").href = payload.open_radar_url;
    const whyList = document.getElementById("whyList");
    whyList.replaceChildren();
    const reasons = [];
    if ((payload.maintainer_activity || "").toLowerCase().includes("high")) reasons.push("Maintainers active");
    if ((payload.categories || []).some(function (value) { return value.toLowerCase().includes("good first issue"); })) reasons.push("Good first issue signal");
    if (payload.merge_probability && payload.merge_probability !== "Unavailable") reasons.push(payload.merge_probability + " merge probability");
    if (payload.contract_potential && payload.contract_potential !== "Unavailable") reasons.push(payload.contract_potential + " contract potential");
    if (!reasons.length && payload.reason) reasons.push(payload.reason);
    reasons.slice(0, 4).forEach(function (reason) {
      const item = document.createElement("li");
      item.textContent = reason;
      whyList.appendChild(item);
    });
    document.getElementById("stale").hidden = !payload.is_stale;
    if (payload.best_issue && payload.best_issue.url) {
      const issue = document.getElementById("bestIssue");
      const link = document.getElementById("bestIssueLink");
      link.href = payload.best_issue.url;
      link.textContent = "#" + payload.best_issue.number + " " + payload.best_issue.title;
      issue.hidden = false;
    }
    show("summary");
  }

  chrome.tabs.query({active: true, currentWindow: true}, function (tabs) {
    const repository = repositoryFromUrl(tabs[0] && tabs[0].url);
    if (!repository) {
      show("unsupported");
      return;
    }
    const endpoint = "https://bashops.site/api/public/repository-summary?owner="
      + encodeURIComponent(repository.owner) + "&repo=" + encodeURIComponent(repository.repo);
    fetch(endpoint, {headers: {Accept: "application/json"}})
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (payload) {
          if (!response.ok || !payload.available) {
            renderUnavailable(repository, payload);
            return;
          }
          renderSummary(payload);
        });
      })
      .catch(function () { renderUnavailable(repository, null); });
  });
})();
