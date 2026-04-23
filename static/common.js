window.KilnUI = {
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
    const payload = await response.json();
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
