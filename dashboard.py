from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from alerts import AlertRule, validate_rule
from config import DATABASE_PATH


HOST = "0.0.0.0"
PORT = 8080
HISTORY_WINDOWS = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}
MAX_HISTORY_ROWS = 5000

PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Kiln Monitor Dashboard</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, sans-serif;
      background: #0f172a;
      color: #e5e7eb;
    }
    body {
      margin: 0;
      padding: 24px;
    }
    main {
      max-width: 1100px;
      margin: 0 auto;
    }
    h1 {
      margin: 0 0 16px;
      font-size: 2rem;
    }
    .status-banner {
      padding: 14px 16px;
      border-radius: 14px;
      margin-bottom: 16px;
      font-weight: 700;
      background: #334155;
    }
    .status-ok {
      background: #065f46;
    }
    .status-error {
      background: #991b1b;
    }
    .cards {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin-bottom: 20px;
    }
    .card {
      background: #111827;
      border: 1px solid #1f2937;
      border-radius: 16px;
      padding: 16px;
    }
    .label {
      color: #9ca3af;
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      margin-bottom: 8px;
    }
    .value {
      font-size: 1.35rem;
      font-weight: 700;
      word-break: break-word;
    }
    .chart-panel {
      background: #111827;
      border: 1px solid #1f2937;
      border-radius: 16px;
      padding: 16px;
    }
    .chart-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .range-buttons {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    button {
      border: 1px solid #334155;
      background: #1f2937;
      color: #e5e7eb;
      border-radius: 999px;
      padding: 8px 14px;
      cursor: pointer;
      font-weight: 600;
    }
    button.active {
      background: #2563eb;
      border-color: #2563eb;
    }
    canvas {
      width: 100%;
      height: 420px;
      display: block;
    }
    .subtle {
      color: #9ca3af;
      font-size: 0.9rem;
      margin-top: 10px;
    }
    .rules-panel {
      background: #111827;
      border: 1px solid #1f2937;
      border-radius: 16px;
      padding: 16px;
      margin-top: 20px;
    }
    .rules-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      margin-bottom: 16px;
    }
    label {
      display: block;
      color: #9ca3af;
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 6px;
    }
    input, select {
      width: 100%;
      box-sizing: border-box;
      border: 1px solid #334155;
      background: #0f172a;
      color: #e5e7eb;
      border-radius: 10px;
      padding: 10px 12px;
    }
    .rule-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 16px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    th, td {
      text-align: left;
      padding: 10px 8px;
      border-top: 1px solid #1f2937;
      font-size: 0.95rem;
      vertical-align: top;
    }
    .pill {
      display: inline-block;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 0.8rem;
      font-weight: 700;
      background: #334155;
    }
    .pill-on {
      background: #065f46;
    }
    .pill-off {
      background: #475569;
    }
    .pill-active {
      background: #7c2d12;
    }
    .error-text {
      color: #fca5a5;
      min-height: 1.2em;
    }
  </style>
</head>
<body>
  <main>
    <h1>Kiln Monitor</h1>
    <div id="statusBanner" class="status-banner">Loading...</div>

    <section class="cards">
      <div class="card">
        <div class="label">Latest Temperature</div>
        <div class="value" id="latestTemp">--</div>
      </div>
      <div class="card">
        <div class="label">Last Update</div>
        <div class="value" id="lastUpdate">--</div>
      </div>
      <div class="card">
        <div class="label">Sample Age</div>
        <div class="value" id="sampleAge">--</div>
      </div>
      <div class="card">
        <div class="label">Last Fault</div>
        <div class="value" id="lastFault">--</div>
      </div>
      <div class="card">
        <div class="label">Total Rows</div>
        <div class="value" id="totalRows">--</div>
      </div>
      <div class="card">
        <div class="label">Last Alert</div>
        <div class="value" id="lastAlert">--</div>
      </div>
    </section>

    <section class="chart-panel">
      <div class="chart-top">
        <div>
          <div class="label">Temperature Trend</div>
          <div class="subtle" id="chartMeta">--</div>
        </div>
        <div class="range-buttons">
          <button type="button" id="smoothToggle" class="active">Smooth</button>
          <button type="button" data-range="1h">1h</button>
          <button type="button" data-range="24h" class="active">24h</button>
          <button type="button" data-range="7d">7d</button>
        </div>
      </div>
      <canvas id="tempChart"></canvas>
      <div class="subtle">Red dots mark fault samples. Gaps show periods where no valid temperature was logged.</div>
    </section>

    <section class="rules-panel">
      <div class="chart-top">
        <div>
          <div class="label">Alert Rules</div>
          <div class="subtle">Configure milestone alerts and high/low safety alerts from the browser.</div>
        </div>
      </div>

      <form id="ruleForm">
        <div class="rules-grid">
          <div>
            <label for="ruleName">Name</label>
            <input id="ruleName" name="name" placeholder="Cone 06 reached" required />
          </div>
          <div>
            <label for="ruleType">Type</label>
            <select id="ruleType" name="rule_type">
              <option value="TARGET_REACHED">Target Reached</option>
              <option value="ABOVE_HIGH">Above High</option>
              <option value="BELOW_LOW">Below Low</option>
            </select>
          </div>
          <div>
            <label for="ruleThreshold">Threshold F</label>
            <input id="ruleThreshold" name="threshold_f" type="number" step="0.1" required />
          </div>
          <div>
            <label for="ruleSeverity">Severity</label>
            <select id="ruleSeverity" name="severity">
              <option value="INFO">Info</option>
              <option value="WARNING" selected>Warning</option>
              <option value="CRITICAL">Critical</option>
            </select>
          </div>
          <div>
            <label for="ruleHysteresis">Reset Gap F</label>
            <input id="ruleHysteresis" name="hysteresis_f" type="number" step="0.1" value="5" required />
          </div>
          <div>
            <label for="ruleEnabled">Enabled</label>
            <select id="ruleEnabled" name="enabled">
              <option value="true" selected>Enabled</option>
              <option value="false">Disabled</option>
            </select>
          </div>
        </div>
        <div class="rule-actions">
          <button type="submit" id="ruleSubmit">Add Rule</button>
          <button type="button" id="ruleCancel">Cancel Edit</button>
        </div>
        <div id="ruleError" class="error-text"></div>
      </form>

      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Threshold</th>
            <th>Severity</th>
            <th>Status</th>
            <th>Last Triggered</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="rulesTableBody">
          <tr><td colspan="7" class="subtle">Loading rules...</td></tr>
        </tbody>
      </table>
    </section>
  </main>

  <script>
    const banner = document.getElementById("statusBanner");
    const latestTemp = document.getElementById("latestTemp");
    const lastUpdate = document.getElementById("lastUpdate");
    const sampleAge = document.getElementById("sampleAge");
    const lastFault = document.getElementById("lastFault");
    const totalRows = document.getElementById("totalRows");
    const lastAlert = document.getElementById("lastAlert");
    const chartMeta = document.getElementById("chartMeta");
    const ruleForm = document.getElementById("ruleForm");
    const ruleSubmit = document.getElementById("ruleSubmit");
    const ruleCancel = document.getElementById("ruleCancel");
    const ruleError = document.getElementById("ruleError");
    const rulesTableBody = document.getElementById("rulesTableBody");
    const canvas = document.getElementById("tempChart");
    const ctx = canvas.getContext("2d");
    let selectedRange = "24h";
    let hoverX = null;
    let smoothingEnabled = true;
    let editingRuleId = null;
    let chartState = {
      points: [],
      plotPoints: [],
    };

    function formatTimestamp(isoText) {
      if (!isoText) {
        return "--";
      }
      return new Date(isoText).toLocaleString();
    }

    function humanizeRuleType(ruleType) {
      if (ruleType === "TARGET_REACHED") {
        return "Target";
      }
      if (ruleType === "ABOVE_HIGH") {
        return "High";
      }
      if (ruleType === "BELOW_LOW") {
        return "Low";
      }
      return ruleType;
    }

    function resetRuleForm() {
      editingRuleId = null;
      ruleForm.reset();
      document.getElementById("ruleSeverity").value = "WARNING";
      document.getElementById("ruleEnabled").value = "true";
      document.getElementById("ruleHysteresis").value = "5";
      ruleSubmit.textContent = "Add Rule";
      ruleError.textContent = "";
    }

    function resizeCanvas() {
      const rect = canvas.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.floor(rect.width * ratio);
      canvas.height = Math.floor(rect.height * ratio);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    }

    function drawRoundedRect(x, y, width, height, radius) {
      ctx.beginPath();
      ctx.moveTo(x + radius, y);
      ctx.lineTo(x + width - radius, y);
      ctx.quadraticCurveTo(x + width, y, x + width, y + radius);
      ctx.lineTo(x + width, y + height - radius);
      ctx.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
      ctx.lineTo(x + radius, y + height);
      ctx.quadraticCurveTo(x, y + height, x, y + height - radius);
      ctx.lineTo(x, y + radius);
      ctx.quadraticCurveTo(x, y, x + radius, y);
      ctx.closePath();
    }

    function smoothPoints(points, windowSize) {
      const smoothed = [];
      const recentTemps = [];

      points.forEach((point) => {
        if (point.status !== "OK" || point.temp_f === null || !Number.isFinite(point.temp_f)) {
          recentTemps.length = 0;
          smoothed.push(point);
          return;
        }

        recentTemps.push(point.temp_f);
        if (recentTemps.length > windowSize) {
          recentTemps.shift();
        }

        const averageF = recentTemps.reduce((sum, temp) => sum + temp, 0) / recentTemps.length;
        smoothed.push({
          ...point,
          temp_f: averageF,
          temp_c: (averageF - 32.0) * 5.0 / 9.0,
        });
      });

      return smoothed;
    }

    function drawHoverOverlay() {
      if (hoverX === null || !chartState.plotPoints.length) {
        return;
      }

      let nearest = chartState.plotPoints[0];
      let nearestDistance = Math.abs(nearest.x - hoverX);

      chartState.plotPoints.forEach((point) => {
        const distance = Math.abs(point.x - hoverX);
        if (distance < nearestDistance) {
          nearest = point;
          nearestDistance = distance;
        }
      });

      const boxWidth = 210;
      const boxHeight = 58;
      const boxX = Math.min(
        Math.max(12, nearest.x + 12),
        canvas.getBoundingClientRect().width - boxWidth - 12,
      );
      const boxY = Math.max(12, nearest.y - boxHeight - 16);

      ctx.strokeStyle = "#f8fafc";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(nearest.x, chartState.top);
      ctx.lineTo(nearest.x, chartState.top + chartState.plotHeight);
      ctx.stroke();

      ctx.fillStyle = nearest.status === "OK" ? "#38bdf8" : "#ef4444";
      ctx.beginPath();
      ctx.arc(nearest.x, nearest.y, 5, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = "rgba(15, 23, 42, 0.96)";
      ctx.strokeStyle = "#64748b";
      ctx.lineWidth = 1;
      drawRoundedRect(boxX, boxY, boxWidth, boxHeight, 10);
      ctx.fill();
      ctx.stroke();

      ctx.fillStyle = "#f8fafc";
      ctx.font = "700 14px system-ui, sans-serif";
      const valueText = nearest.status === "OK"
        ? `${nearest.temp_f.toFixed(1)} F / ${nearest.temp_c.toFixed(1)} C`
        : `ERROR: ${nearest.detail || "fault"}`;
      ctx.fillText(valueText, boxX + 12, boxY + 23);

      ctx.fillStyle = "#cbd5e1";
      ctx.font = "12px system-ui, sans-serif";
      ctx.fillText(formatTimestamp(nearest.timestamp_utc), boxX + 12, boxY + 42);
    }

    function drawChart(points) {
      const displayPoints = smoothingEnabled
        ? smoothPoints(points, 12)
        : points;

      resizeCanvas();
      const width = canvas.getBoundingClientRect().width;
      const height = canvas.getBoundingClientRect().height;
      ctx.clearRect(0, 0, width, height);

      const left = 64;
      const right = 16;
      const top = 16;
      const bottom = 36;
      const plotWidth = Math.max(1, width - left - right);
      const plotHeight = Math.max(1, height - top - bottom);

      chartState = {
        points,
        plotPoints: [],
        top,
        plotHeight,
      };

      ctx.fillStyle = "#0f172a";
      ctx.fillRect(0, 0, width, height);
      ctx.strokeStyle = "#334155";
      ctx.lineWidth = 1;
      ctx.strokeRect(left, top, plotWidth, plotHeight);

      if (!displayPoints.length) {
        ctx.fillStyle = "#9ca3af";
        ctx.font = "14px system-ui, sans-serif";
        ctx.fillText("No samples in this window yet.", left + 12, top + 28);
        chartMeta.textContent = "No samples available.";
        return;
      }

      const times = displayPoints.map((point) => new Date(point.timestamp_utc).getTime());
      const validTemps = displayPoints
        .map((point) => point.temp_f)
        .filter((temp) => temp !== null && Number.isFinite(temp));
      const minTime = Math.min(...times);
      const maxTime = Math.max(...times);
      const minTemp = validTemps.length ? Math.min(...validTemps) : 0;
      const maxTemp = validTemps.length ? Math.max(...validTemps) : 1;
      let paddedMinTemp = minTemp === maxTemp ? minTemp - 1 : minTemp - Math.max(1, (maxTemp - minTemp) * 0.08);
      let paddedMaxTemp = minTemp === maxTemp ? maxTemp + 1 : maxTemp + Math.max(1, (maxTemp - minTemp) * 0.08);
      if ((paddedMaxTemp - paddedMinTemp) < 20.0) {
        const midpoint = (paddedMinTemp + paddedMaxTemp) / 2;
        paddedMinTemp = midpoint - (20.0 / 2);
        paddedMaxTemp = midpoint + (20.0 / 2);
      }
      const timeSpan = Math.max(1, maxTime - minTime);
      const tempSpan = Math.max(1, paddedMaxTemp - paddedMinTemp);

      function xFor(pointTime) {
        return left + ((pointTime - minTime) / timeSpan) * plotWidth;
      }

      function yFor(tempF) {
        return top + plotHeight - ((tempF - paddedMinTemp) / tempSpan) * plotHeight;
      }

      ctx.strokeStyle = "#1f2937";
      ctx.fillStyle = "#9ca3af";
      ctx.font = "12px system-ui, sans-serif";
      for (let step = 0; step <= 4; step += 1) {
        const y = top + (plotHeight * step) / 4;
        const tempLabel = paddedMaxTemp - (tempSpan * step) / 4;
        ctx.beginPath();
        ctx.moveTo(left, y);
        ctx.lineTo(left + plotWidth, y);
        ctx.stroke();
        ctx.fillText(`${tempLabel.toFixed(0)} F`, 8, y + 4);
      }

      let segmentOpen = false;
      ctx.strokeStyle = "#38bdf8";
      ctx.lineWidth = 2;
      ctx.beginPath();

      displayPoints.forEach((point) => {
        const pointTime = new Date(point.timestamp_utc).getTime();
        if (point.temp_f === null || !Number.isFinite(point.temp_f) || point.status !== "OK") {
          segmentOpen = false;
          if (point.status === "ERROR") {
            chartState.plotPoints.push({
              ...point,
              x: xFor(pointTime),
              y: top + plotHeight - 5,
            });
          }
          return;
        }
        const x = xFor(pointTime);
        const y = yFor(point.temp_f);
        chartState.plotPoints.push({
          ...point,
          x,
          y,
        });
        if (!segmentOpen) {
          ctx.moveTo(x, y);
          segmentOpen = true;
        } else {
          ctx.lineTo(x, y);
        }
      });
      ctx.stroke();

      displayPoints.forEach((point) => {
        if (point.status !== "ERROR") {
          return;
        }
        const x = xFor(new Date(point.timestamp_utc).getTime());
        ctx.fillStyle = "#ef4444";
        ctx.beginPath();
        ctx.arc(x, top + plotHeight - 5, 4, 0, Math.PI * 2);
        ctx.fill();
      });

      ctx.fillStyle = "#9ca3af";
      ctx.fillText(formatTimestamp(displayPoints[0].timestamp_utc), left, height - 12);
      const endLabel = formatTimestamp(displayPoints[displayPoints.length - 1].timestamp_utc);
      const endWidth = ctx.measureText(endLabel).width;
      ctx.fillText(endLabel, left + plotWidth - endWidth, height - 12);

      if (validTemps.length) {
        const modeLabel = smoothingEnabled ? "smoothed" : "raw";
        chartMeta.textContent = `${displayPoints.length} samples, ${minTemp.toFixed(1)} F to ${maxTemp.toFixed(1)} F, ${modeLabel}`;
      } else {
        chartMeta.textContent = `${displayPoints.length} samples, no valid temperatures in range`;
      }

      drawHoverOverlay();
    }

    async function refreshStatus() {
      const response = await fetch("/api/status");
      const payload = await response.json();

      totalRows.textContent = payload.total_rows.toLocaleString();

      if (!payload.latest_sample) {
        banner.textContent = "No samples logged yet";
        banner.className = "status-banner";
        latestTemp.textContent = "--";
        lastUpdate.textContent = "--";
        sampleAge.textContent = "--";
        lastFault.textContent = payload.latest_fault ? payload.latest_fault.detail : "none";
        lastAlert.textContent = payload.latest_alert ? payload.latest_alert.detail : "none";
        return;
      }

      const latest = payload.latest_sample;
      const isOk = latest.status === "OK";
      banner.textContent = isOk ? "Sensor OK" : `Sensor ERROR: ${latest.detail || "fault sample logged"}`;
      banner.className = `status-banner ${isOk ? "status-ok" : "status-error"}`;
      latestTemp.textContent = latest.temp_f === null ? "--" : `${latest.temp_f.toFixed(1)} F / ${latest.temp_c.toFixed(1)} C`;
      lastUpdate.textContent = formatTimestamp(latest.timestamp_utc);
      sampleAge.textContent = latest.sample_age;
      lastFault.textContent = payload.latest_fault
        ? `${payload.latest_fault.sample_age} ago: ${payload.latest_fault.detail || "fault"}`
        : "none";
      lastAlert.textContent = payload.latest_alert
        ? `${payload.latest_alert.level}: ${payload.latest_alert.rule_name || payload.latest_alert.kind.toLowerCase()}`
        : "none";
    }

    async function refreshAlertRules() {
      const response = await fetch("/api/alert-rules");
      const payload = await response.json();
      const rules = payload.rules || [];

      if (!rules.length) {
        rulesTableBody.innerHTML = '<tr><td colspan="7" class="subtle">No alert rules configured yet.</td></tr>';
        return;
      }

      rulesTableBody.innerHTML = "";
      rules.forEach((rule) => {
        const row = document.createElement("tr");
        row.innerHTML = `
          <td>${rule.name}<div class="subtle">reset gap ${rule.hysteresis_f.toFixed(1)} F</div></td>
          <td>${humanizeRuleType(rule.rule_type)}</td>
          <td>${rule.threshold_f.toFixed(1)} F</td>
          <td>${rule.severity}</td>
          <td>
            <span class="pill ${rule.enabled ? "pill-on" : "pill-off"}">${rule.enabled ? "Enabled" : "Disabled"}</span>
            ${rule.active ? '<span class="pill pill-active">Active</span>' : ""}
          </td>
          <td>${formatTimestamp(rule.last_triggered_at)}</td>
          <td>
            <button type="button" data-edit="${rule.id}">Edit</button>
            <button type="button" data-delete="${rule.id}">Delete</button>
          </td>
        `;
        rulesTableBody.appendChild(row);
      });

      rulesTableBody.querySelectorAll("[data-edit]").forEach((button) => {
        button.addEventListener("click", () => {
          const rule = rules.find((item) => item.id === Number(button.dataset.edit));
          if (!rule) {
            return;
          }
          editingRuleId = rule.id;
          document.getElementById("ruleName").value = rule.name;
          document.getElementById("ruleType").value = rule.rule_type;
          document.getElementById("ruleThreshold").value = rule.threshold_f;
          document.getElementById("ruleSeverity").value = rule.severity;
          document.getElementById("ruleHysteresis").value = rule.hysteresis_f;
          document.getElementById("ruleEnabled").value = rule.enabled ? "true" : "false";
          ruleSubmit.textContent = "Save Rule";
          ruleError.textContent = "";
        });
      });

      rulesTableBody.querySelectorAll("[data-delete]").forEach((button) => {
        button.addEventListener("click", async () => {
          await fetch(`/api/alert-rules/${button.dataset.delete}/delete`, { method: "POST" });
          if (editingRuleId === Number(button.dataset.delete)) {
            resetRuleForm();
          }
          await refreshAlertRules();
        });
      });
    }

    async function refreshHistory() {
      const response = await fetch(`/api/history?range=${encodeURIComponent(selectedRange)}`);
      const payload = await response.json();
      drawChart(payload.samples);
    }

    async function refreshAll() {
      try {
        await Promise.all([refreshStatus(), refreshHistory(), refreshAlertRules()]);
      } catch (error) {
        banner.textContent = `Dashboard refresh failed: ${error}`;
        banner.className = "status-banner status-error";
      }
    }

    document.querySelectorAll("button[data-range]").forEach((button) => {
      button.addEventListener("click", async () => {
        selectedRange = button.dataset.range;
        document.querySelectorAll("button[data-range]").forEach((item) => {
          item.classList.toggle("active", item === button);
        });
        await refreshHistory();
      });
    });

    document.getElementById("smoothToggle").addEventListener("click", (event) => {
      smoothingEnabled = !smoothingEnabled;
      event.target.classList.toggle("active", smoothingEnabled);
      event.target.textContent = smoothingEnabled ? "Smooth" : "Raw";
      drawChart(chartState.points);
    });

    ruleForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      ruleError.textContent = "";
      const payload = {
        name: document.getElementById("ruleName").value.trim(),
        rule_type: document.getElementById("ruleType").value,
        threshold_f: Number(document.getElementById("ruleThreshold").value),
        severity: document.getElementById("ruleSeverity").value,
        hysteresis_f: Number(document.getElementById("ruleHysteresis").value),
        enabled: document.getElementById("ruleEnabled").value === "true",
      };

      const path = editingRuleId === null
        ? "/api/alert-rules"
        : `/api/alert-rules/${editingRuleId}`;

      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok) {
        ruleError.textContent = result.error || "Failed to save alert rule.";
        return;
      }

      resetRuleForm();
      await refreshAlertRules();
    });

    ruleCancel.addEventListener("click", () => {
      resetRuleForm();
    });

    canvas.addEventListener("mousemove", (event) => {
      hoverX = event.clientX - canvas.getBoundingClientRect().left;
      drawChart(chartState.points);
    });

    canvas.addEventListener("mouseleave", () => {
      hoverX = null;
      drawChart(chartState.points);
    });

    window.addEventListener("resize", refreshHistory);
    resetRuleForm();
    refreshAll();
    setInterval(refreshAll, 5000);
  </script>
</body>
</html>
"""


def format_sample_age(timestamp_utc: str) -> str:
    sample_time = datetime.fromisoformat(timestamp_utc)
    age_seconds = (datetime.now(timezone.utc) - sample_time).total_seconds()
    if age_seconds < 0:
        return "0s"
    if age_seconds < 60:
        return f"{int(age_seconds)}s"
    if age_seconds < 3600:
        return f"{int(age_seconds // 60)}m {int(age_seconds % 60)}s"
    return f"{int(age_seconds // 3600)}h {int((age_seconds % 3600) // 60)}m"


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def table_has_column(connection: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(column["name"] == column_name for column in columns)


def open_readonly_connection() -> sqlite3.Connection | None:
    if not DATABASE_PATH.exists():
        return None

    connection = sqlite3.connect(f"file:{DATABASE_PATH}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000;")
    return connection


def open_readwrite_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000;")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            rule_type TEXT NOT NULL,
            threshold_f REAL NOT NULL,
            severity TEXT NOT NULL,
            hysteresis_f REAL NOT NULL DEFAULT 5.0,
            active INTEGER NOT NULL DEFAULT 0,
            last_triggered_at TEXT
        )
        """
    )
    connection.commit()
    return connection


def fetch_dashboard_status() -> dict:
    connection = open_readonly_connection()
    if connection is None:
        return {
            "database_path": str(DATABASE_PATH),
            "total_rows": 0,
            "latest_sample": None,
            "latest_fault": None,
            "latest_alert": None,
        }

    try:
        latest_sample = connection.execute(
            """
            SELECT id, timestamp_utc, temp_c, temp_f, status, detail
            FROM temperature_log
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        latest_fault = connection.execute(
            """
            SELECT id, timestamp_utc, detail
            FROM temperature_log
            WHERE status = 'ERROR'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        latest_alert = None
        if table_exists(connection, "alert_log"):
            latest_alert_select = "id, timestamp_utc, level, kind, detail, temp_c, temp_f"
            if table_has_column(connection, "alert_log", "rule_name"):
                latest_alert_select += ", rule_name"
            latest_alert = connection.execute(
                f"""
                SELECT {latest_alert_select}
                FROM alert_log
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        total_rows = connection.execute("SELECT COUNT(*) FROM temperature_log").fetchone()[0]
    finally:
        connection.close()

    return {
        "database_path": str(DATABASE_PATH),
        "total_rows": total_rows,
        "latest_sample": row_to_payload(latest_sample),
        "latest_fault": row_to_payload(latest_fault),
        "latest_alert": row_to_payload(latest_alert),
    }


def fetch_history(window_name: str) -> dict:
    if window_name not in HISTORY_WINDOWS:
        window_name = "24h"

    connection = open_readonly_connection()
    if connection is None:
        return {"range": window_name, "samples": []}

    cutoff = (datetime.now(timezone.utc) - HISTORY_WINDOWS[window_name]).isoformat()
    try:
        rows = connection.execute(
            """
            SELECT id, timestamp_utc, temp_c, temp_f, status, detail
            FROM (
                SELECT id, timestamp_utc, temp_c, temp_f, status, detail
                FROM temperature_log
                WHERE timestamp_utc >= ?
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id ASC
            """,
            (cutoff, MAX_HISTORY_ROWS),
        ).fetchall()
    finally:
        connection.close()

    return {
        "range": window_name,
        "samples": [row_to_payload(row) for row in rows],
    }


def fetch_alert_rules() -> dict:
    if not DATABASE_PATH.exists():
        return {"rules": []}

    connection = open_readonly_connection()
    if connection is None or not table_exists(connection, "alert_rules"):
        if connection is not None:
            connection.close()
        return {"rules": []}

    try:
        rows = connection.execute(
            """
            SELECT id, name, enabled, rule_type, threshold_f, severity, hysteresis_f, active, last_triggered_at
            FROM alert_rules
            ORDER BY threshold_f ASC, id ASC
            """
        ).fetchall()
    finally:
        connection.close()

    return {
        "rules": [
            {
                "id": row["id"],
                "name": row["name"],
                "enabled": bool(row["enabled"]),
                "rule_type": row["rule_type"],
                "threshold_f": row["threshold_f"],
                "severity": row["severity"],
                "hysteresis_f": row["hysteresis_f"],
                "active": bool(row["active"]),
                "last_triggered_at": row["last_triggered_at"],
            }
            for row in rows
        ]
    }


def parse_alert_rule_payload(payload: dict) -> AlertRule:
    rule = AlertRule(
        id=None,
        name=str(payload.get("name", "")).strip(),
        enabled=bool(payload.get("enabled", True)),
        rule_type=str(payload.get("rule_type", "")).strip().upper(),
        threshold_f=float(payload.get("threshold_f")),
        severity=str(payload.get("severity", "")).strip().upper(),
        hysteresis_f=float(payload.get("hysteresis_f", 0.0)),
        active=False,
        last_triggered_at=None,
    )
    validate_rule(rule)
    return rule


def create_alert_rule(payload: dict) -> dict:
    rule = parse_alert_rule_payload(payload)
    connection = open_readwrite_connection()
    try:
        connection.execute(
            """
            INSERT INTO alert_rules (
                name,
                enabled,
                rule_type,
                threshold_f,
                severity,
                hysteresis_f,
                active,
                last_triggered_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL)
            """,
            (
                rule.name,
                int(rule.enabled),
                rule.rule_type,
                rule.threshold_f,
                rule.severity,
                rule.hysteresis_f,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return {"ok": True}


def update_alert_rule(rule_id: int, payload: dict) -> dict:
    rule = parse_alert_rule_payload(payload)
    connection = open_readwrite_connection()
    try:
        current_row = connection.execute(
            """
            SELECT active, last_triggered_at
            FROM alert_rules
            WHERE id = ?
            """,
            (rule_id,),
        ).fetchone()
        if current_row is None:
            raise ValueError("alert rule not found")

        connection.execute(
            """
            UPDATE alert_rules
            SET name = ?, enabled = ?, rule_type = ?, threshold_f = ?, severity = ?, hysteresis_f = ?,
                active = CASE WHEN ? = 1 THEN active ELSE 0 END
            WHERE id = ?
            """,
            (
                rule.name,
                int(rule.enabled),
                rule.rule_type,
                rule.threshold_f,
                rule.severity,
                rule.hysteresis_f,
                int(rule.enabled),
                rule_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return {"ok": True}


def delete_alert_rule(rule_id: int) -> dict:
    connection = open_readwrite_connection()
    try:
        connection.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
        connection.commit()
    finally:
        connection.close()
    return {"ok": True}


def row_to_payload(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None

    payload = {
        "id": row["id"],
        "timestamp_utc": row["timestamp_utc"],
        "detail": row["detail"],
    }
    if "temp_c" in row.keys():
        payload["temp_c"] = row["temp_c"]
    if "temp_f" in row.keys():
        payload["temp_f"] = row["temp_f"]
    if "status" in row.keys():
        payload["status"] = row["status"]
    if "level" in row.keys():
        payload["level"] = row["level"]
    if "kind" in row.keys():
        payload["kind"] = row["kind"]
    if "rule_name" in row.keys():
        payload["rule_name"] = row["rule_name"]
    payload["sample_age"] = format_sample_age(row["timestamp_utc"])
    return payload


class DashboardRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed_path = urlparse(self.path)

        if parsed_path.path == "/":
            self.send_text_response(PAGE_HTML, content_type="text/html; charset=utf-8")
            return

        if parsed_path.path == "/api/status":
            self.send_json_response(fetch_dashboard_status())
            return

        if parsed_path.path == "/api/history":
            query = parse_qs(parsed_path.query)
            range_name = query.get("range", ["24h"])[0]
            self.send_json_response(fetch_history(range_name))
            return

        if parsed_path.path == "/api/alert-rules":
            self.send_json_response(fetch_alert_rules())
            return

        self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        parsed_path = urlparse(self.path)

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length) if content_length else b"{}"
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self.send_json_response({"error": "Invalid JSON body."}, status=400)
            return

        try:
            if parsed_path.path == "/api/alert-rules":
                self.send_json_response(create_alert_rule(payload))
                return

            if parsed_path.path.startswith("/api/alert-rules/") and parsed_path.path.endswith("/delete"):
                rule_id = int(parsed_path.path.split("/")[3])
                self.send_json_response(delete_alert_rule(rule_id))
                return

            if parsed_path.path.startswith("/api/alert-rules/"):
                rule_id = int(parsed_path.path.split("/")[3])
                self.send_json_response(update_alert_rule(rule_id, payload))
                return
        except ValueError as exc:
            self.send_json_response({"error": str(exc)}, status=400)
            return
        except sqlite3.Error as exc:
            self.send_json_response({"error": f"Database error: {exc}"}, status=500)
            return

        self.send_json_response({"error": "Not Found"}, status=404)

    def log_message(self, format: str, *args) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        print(f"{timestamp} | dashboard | {format % args}")

    def send_json_response(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text_response(self, body_text: str, content_type: str) -> None:
        body = body_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kiln monitor dashboard")
    parser.add_argument("--host", default=HOST, help="Bind host for the dashboard server.")
    parser.add_argument("--port", type=int, default=PORT, help="Bind port for the dashboard server.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DashboardRequestHandler)
    print(f"Kiln dashboard serving http://{args.host}:{args.port}")
    print(f"Reading database: {DATABASE_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Dashboard stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
