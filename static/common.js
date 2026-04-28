window.KilnUI = {
  pages: [
    { href: "/", key: "dashboard", label: "Dashboard" },
    { href: "/panel", key: "panel", label: "Panel" },
    { href: "/logs", key: "logs", label: "Firing Logs" },
    { href: "/alerts", key: "alerts", label: "Alerts" },
    { href: "/events", key: "events", label: "Events" },
    { href: "/faults", key: "faults", label: "Faults" },
  ],

  renderPageHeaders() {
    document.querySelectorAll("[data-app-header]").forEach((container) => {
      const activePage = container.dataset.activePage || "";
      const title = container.dataset.title || "Kiln Monitor";
      const subtitle = container.dataset.subtitle || "";
      const actionsTemplate = container.querySelector("template[data-header-actions]");
      const actionsMarkup = actionsTemplate ? actionsTemplate.innerHTML.trim() : "";
      const navMarkup = this.pages.map((page) => {
        const activeClass = page.key === activePage ? " active-page" : "";
        return `<a href="${page.href}" class="page-link${activeClass}">${page.label}</a>`;
      }).join("");

      container.innerHTML = `
        <section class="page-nav" aria-label="Pages">${navMarkup}</section>
        <section class="page-header">
          <div>
            <h1>${title}</h1>
            ${subtitle ? `<div class="subtle">${subtitle}</div>` : ""}
          </div>
          ${actionsMarkup ? `<div class="header-actions toolbar">${actionsMarkup}</div>` : ""}
        </section>
      `;
    });
  },

  formatTimestamp(isoText, options) {
    if (!isoText) {
      return "--";
    }
    return new Date(isoText).toLocaleString([], options);
  },

  formatTime(isoText, options) {
    if (!isoText) {
      return "--";
    }
    return new Date(isoText).toLocaleTimeString([], options);
  },

  humanizeRuleType(ruleType) {
    if (ruleType === "TARGET_REACHED") {
      return "Target";
    }
    if (ruleType === "ABOVE_HIGH") {
      return "High";
    }
    if (ruleType === "BELOW_LOW") {
      return "Low";
    }
    if (ruleType === "TIME_ELAPSED") {
      return "Elapsed";
    }
    return ruleType;
  },

  async fetchJson(url, options) {
    const response = await fetch(url, options);
    let payload = {};
    try {
      payload = await response.json();
    } catch (_error) {
      payload = {};
    }
    if (!response.ok) {
      throw new Error(payload.error || `Request failed: ${response.status}`);
    }
    return payload;
  },

  async postJson(url, body) {
    return this.fetchJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  },
};

window.KilnUI.renderPageHeaders();
