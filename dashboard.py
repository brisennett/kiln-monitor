from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from alerts import AlertEvent, AlertRule, validate_rule
from camera import CameraError, capture_snapshot, latest_snapshot_info
from config import (
    CAMERA_SNAPSHOTS_DIR,
    DATABASE_PATH,
    WATCHDOG_FAULT_STREAK_THRESHOLD,
    WATCHDOG_NOTIFY_COOLDOWN_MINUTES,
    WATCHDOG_STALE_DATA_SECONDS,
)
from notifiers import NotificationError, build_enabled_notifiers, default_alert_channel_settings, load_alert_channel_settings
from profiles import (
    FiringProfile,
    ProfileSegment,
    expected_profile_state,
    generate_profile_overlay,
    profile_to_payload,
    validate_profile,
)
from storage.sqlite_logger import SQLiteLogger


HOST = "0.0.0.0"
PORT = 8080
HISTORY_WINDOWS = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}
HISTORY_BUCKET_PRESETS = {
    "1h": {
        "auto_bucket_seconds": 2,
        "resolution_options": [2, 10, 30, 60, 300],
    },
    "24h": {
        "auto_bucket_seconds": 600,
        "resolution_options": [60, 300, 600, 900, 1800],
    },
    "7d": {
        "auto_bucket_seconds": 1800,
        "resolution_options": [300, 600, 1800, 3600, 10800],
    },
}

PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Kiln Monitor Dashboard</title>
  <style>
    :root {
      color-scheme: dark;
      font-family: "Avenir Next", "Segoe UI", ui-sans-serif, system-ui, sans-serif;
      background: var(--page-bg);
      color: #e5e7eb;
      --accent-color: #38bdf8;
      --accent-soft: rgba(56, 189, 248, 0.18);
      --page-bg: #0b1220;
      --page-bg-secondary: #131d31;
      --panel-bg: #111827;
      --panel-border: rgba(56, 189, 248, 0.18);
    }
    body {
      margin: 0;
      padding: 24px;
      background:
        radial-gradient(circle at top left, rgba(56, 189, 248, 0.10), transparent 28%),
        linear-gradient(180deg, var(--page-bg-secondary), var(--page-bg));
      min-height: 100vh;
    }
    main {
      max-width: 1400px;
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
      border: 1px solid var(--accent-color);
      box-shadow: inset 0 0 0 1px var(--accent-soft);
    }
    .status-ok {
      background: #065f46;
    }
    .status-error {
      background: #991b1b;
    }
    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
      justify-content: flex-end;
      margin-bottom: 16px;
      padding: 0;
    }
    .cards {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin-bottom: 20px;
    }
    .main-column {
      display: flex;
      flex-direction: column;
      gap: 18px;
      min-width: 0;
    }
    .layout-zone {
      display: grid;
      gap: 12px;
      min-height: 72px;
      align-content: start;
    }
    .layout-zone.layout-zone-cards {
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    }
    .layout-zone.drag-target-zone {
      outline: 2px dashed var(--accent-color);
      outline-offset: 8px;
      border-radius: 18px;
    }
    .zone-label {
      color: #94a3b8;
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      margin-bottom: 8px;
    }
    .card {
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-radius: 16px;
      padding: 16px;
      box-shadow: inset 0 0 0 1px rgba(15, 23, 42, 0.45);
      transition: transform 120ms ease, border-color 120ms ease, box-shadow 120ms ease;
    }
    .card.layout-edit {
      cursor: grab;
    }
    .card.dragging {
      opacity: 0.45;
      transform: scale(0.98);
    }
    .card.drag-target {
      border-color: var(--accent-color);
      box-shadow: 0 0 0 2px var(--accent-soft);
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
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
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
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-radius: 16px;
      padding: 16px;
      margin-top: 0;
    }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(2, 6, 23, 0.75);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 18px;
      z-index: 30;
    }
    .modal-backdrop[hidden] {
      display: none;
    }
    .modal-panel {
      width: min(720px, 100%);
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-radius: 18px;
      padding: 18px;
      box-shadow: 0 24px 80px rgba(15, 23, 42, 0.55);
    }
    .modal-header {
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }
    .modal-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      margin-bottom: 16px;
    }
    .modal-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
    }
    .modal-actions-group {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .tab-strip {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 14px;
    }
    .tab-button {
      background: transparent;
      border-color: #334155;
    }
    .tab-button.active {
      background: var(--accent-soft);
      border-color: var(--accent-color);
      color: #f8fafc;
    }
    .tab-panel[hidden] {
      display: none;
    }
    .rules-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: 1fr;
      margin-bottom: 16px;
    }
    .rules-table-wrap {
      overflow-x: auto;
      margin: 0 -4px;
      padding: 0 4px;
    }
    .tab-panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }
    .delivery-summary {
      color: #9ca3af;
      font-size: 0.9rem;
      margin-bottom: 0;
    }
    .delivery-detail {
      min-width: 220px;
      white-space: normal;
      word-break: break-word;
    }
    .channel-badges {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }
    .channel-badge {
      display: inline-block;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 0.75rem;
      font-weight: 700;
      background: rgba(51, 65, 85, 0.9);
      color: #cbd5e1;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .channel-toolbar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }
    .panel-divider {
      border: 0;
      border-top: 1px solid #273449;
      margin: 14px 0;
    }
    .channel-sections {
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .channel-section {
      border: 1px solid #1f2937;
      border-radius: 14px;
      padding: 14px;
      background: rgba(15, 23, 42, 0.5);
    }
    .channel-section-title {
      margin: 0 0 6px;
      font-size: 1rem;
      font-weight: 700;
    }
    .channel-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin-top: 12px;
    }
    .test-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .test-status {
      min-height: 1.2em;
      margin-bottom: 12px;
    }
    .channel-health {
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin-bottom: 14px;
    }
    .channel-status-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 0;
    }
    .channel-status-main {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      min-width: 0;
    }
    .channel-status-name {
      font-weight: 700;
      min-width: 56px;
    }
    .channel-test-button {
      flex: 0 0 auto;
    }
    .pill-warning {
      background: #7c2d12;
    }
    .success-text {
      color: #86efac;
      min-height: 1.2em;
    }
    .watchdog-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin-top: 12px;
    }
    .profiles-panel {
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-radius: 16px;
      padding: 16px;
    }
    .profile-toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      margin-top: 12px;
    }
    .profile-summary {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-top: 12px;
    }
    .profile-summary-card {
      border: 1px solid var(--panel-border);
      border-radius: 14px;
      padding: 12px;
      background: rgba(15, 23, 42, 0.45);
    }
    .profile-summary-card .value {
      font-size: 1rem;
    }
    .profile-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin-top: 12px;
    }
    .profile-segments-table input {
      width: 100%;
      box-sizing: border-box;
    }
    .profile-segments-table td {
      vertical-align: top;
    }
    .profile-list-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .profile-empty {
      color: #9ca3af;
      padding: 12px 0;
    }
    .camera-panel {
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-radius: 16px;
      padding: 16px;
    }
    .camera-shell {
      display: grid;
      grid-template-columns: minmax(260px, 360px) minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }
    #cameraPreviewWrap {
      position: relative;
    }
    .camera-preview {
      width: 100%;
      aspect-ratio: 4 / 3;
      object-fit: cover;
      border-radius: 14px;
      border: 1px solid var(--panel-border);
      background: #020617;
    }
    .camera-empty {
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 240px;
      border-radius: 14px;
      border: 1px dashed #334155;
      color: #94a3b8;
      background: rgba(2, 6, 23, 0.55);
      text-align: center;
      padding: 16px;
    }
    .camera-timestamp {
      position: absolute;
      right: 12px;
      bottom: 12px;
      padding: 8px 10px;
      border-radius: 10px;
      background: rgba(15, 23, 42, 0.82);
      color: #f8fafc;
      font-size: 0.85rem;
      font-weight: 700;
      letter-spacing: 0.02em;
      backdrop-filter: blur(4px);
    }
    .camera-meta {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }
    .camera-meta-card {
      border: 1px solid var(--panel-border);
      border-radius: 14px;
      padding: 12px;
      background: rgba(15, 23, 42, 0.45);
    }
    .camera-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 12px;
    }
    .ops-grid {
      display: grid;
      gap: 16px;
      grid-template-columns: minmax(260px, 360px) minmax(0, 1fr);
    }
    .ops-card {
      background: var(--panel-bg);
      border: 1px solid var(--panel-border);
      border-radius: 16px;
      padding: 16px;
    }
    .ops-list {
      display: grid;
      gap: 10px;
      margin-top: 14px;
      max-height: 280px;
      overflow-y: auto;
    }
    .ops-item {
      border: 1px solid rgba(148, 163, 184, 0.18);
      border-radius: 12px;
      padding: 10px 12px;
      background: rgba(15, 23, 42, 0.38);
    }
    .ops-item-top {
      display: flex;
      gap: 10px;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 6px;
    }
    .ops-item-label {
      font-weight: 700;
    }
    .ops-item-time {
      color: #9ca3af;
      font-size: 0.85rem;
    }
    .ops-item-detail {
      color: #cbd5e1;
      font-size: 0.92rem;
    }
    .event-form-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    }
    .diagnostics-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      margin-top: 14px;
    }
    .diagnostic-card {
      border: 1px solid rgba(148, 163, 184, 0.18);
      border-radius: 12px;
      padding: 12px;
      background: rgba(15, 23, 42, 0.38);
    }
    .diagnostic-card .value {
      font-size: 1.05rem;
    }
    .event-badge {
      background: rgba(56, 189, 248, 0.15);
      color: #bae6fd;
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 0.78rem;
      font-weight: 700;
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
      min-width: 640px;
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
    .color-input {
      height: 46px;
      padding: 6px;
    }
    .color-swatch {
      display: inline-block;
      width: 14px;
      height: 14px;
      border-radius: 999px;
      margin-right: 8px;
      border: 1px solid rgba(255, 255, 255, 0.25);
      vertical-align: middle;
    }
    .card-actions {
      margin-top: 12px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .mini-button {
      padding: 6px 10px;
      font-size: 0.8rem;
    }
    @media (max-width: 720px) {
      body {
        padding: 14px;
      }
      .toolbar {
        padding: 14px;
      }
      .chart-top {
        align-items: flex-start;
        flex-direction: column;
      }
      canvas {
        height: 320px;
      }
      .camera-shell {
        grid-template-columns: 1fr;
      }
      .ops-grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <main>
    <h1>Kiln Monitor</h1>
    <section class="toolbar">
      <button type="button" id="openSetupButton">Dashboard Setup</button>
      <button type="button" id="resetFaultsButton">Reset Faults</button>
      <button type="button" id="resetAlertsButton">Reset Alerts</button>
    </section>
    <div id="statusBanner" class="status-banner">Loading...</div>

    <div id="setupModal" class="modal-backdrop" hidden>
      <section class="modal-panel" role="dialog" aria-modal="true" aria-labelledby="setupModalTitle">
        <div class="modal-header">
          <div>
            <div class="label" id="setupModalTitle">Dashboard Setup</div>
            <div class="subtle">Adjust colors, units, and layout when you need to, then tuck it away.</div>
          </div>
          <button type="button" id="closeSetupButton">Close</button>
        </div>

        <div class="modal-grid">
          <div>
            <label for="accentColorPicker">Accent Color</label>
            <input id="accentColorPicker" class="color-input" type="color" value="#38bdf8" />
          </div>
          <div>
            <label for="pageBgPicker">Page Background</label>
            <input id="pageBgPicker" class="color-input" type="color" value="#0b1220" />
          </div>
          <div>
            <label for="panelBgPicker">Panel Background</label>
            <input id="panelBgPicker" class="color-input" type="color" value="#111827" />
          </div>
          <div>
            <label for="unitSelect">Display Units</label>
            <select id="unitSelect">
              <option value="F">Fahrenheit</option>
              <option value="C">Celsius</option>
              <option value="BOTH">Both</option>
            </select>
          </div>
        </div>

        <div class="modal-actions">
          <div class="modal-actions-group">
            <button type="button" id="layoutToggle">Edit Layout</button>
            <button type="button" id="resetColorsButton">Reset Colors</button>
          </div>
          <div class="subtle">Drag cards between zones while layout edit mode is on.</div>
        </div>
      </section>
    </div>

    <div class="main-column">
        <section>
          <div class="zone-label">Top Summary</div>
          <div class="layout-zone layout-zone-cards" id="topSummaryZone" data-zone-id="top-summary"></div>
        </section>

        <section class="chart-panel">
          <div class="chart-top">
            <div>
              <div class="label">Temperature Trend</div>
              <div class="subtle" id="chartMeta">--</div>
            </div>
            <div class="range-buttons">
              <select id="resolutionSelect" aria-label="Chart resolution">
                <option value="auto">Auto</option>
              </select>
              <button type="button" id="smoothToggle" class="active">Smooth</button>
              <button type="button" data-range="1h">1h</button>
              <button type="button" data-range="24h" class="active">24h</button>
              <button type="button" data-range="7d">7d</button>
            </div>
          </div>
          <canvas id="tempChart"></canvas>
          <div class="subtle">Red dots mark fault samples. Gaps show periods where no valid temperature was logged.</div>
        </section>

        <section class="ops-grid">
          <section class="ops-card">
            <div class="label">Event Markers</div>
            <div class="subtle">Add manual run notes so we can line up wiring, power, and kiln changes with the fault timeline.</div>
            <form id="eventForm">
              <div class="event-form-grid">
                <div>
                  <label for="eventType">Event Type</label>
                  <select id="eventType" name="event_type">
                    <option value="OPERATION">Operation</option>
                    <option value="POWER">Power</option>
                    <option value="OBSERVATION">Observation</option>
                    <option value="NOTE">Note</option>
                  </select>
                </div>
                <div>
                  <label for="eventLabel">Label</label>
                  <input id="eventLabel" name="label" placeholder="Turned third coil on" required />
                </div>
                <div style="grid-column: 1 / -1;">
                  <label for="eventDetail">Detail</label>
                  <input id="eventDetail" name="detail" placeholder="Optional extra note" />
                </div>
              </div>
              <div class="rule-actions">
                <button type="submit">Add Marker</button>
              </div>
              <div id="eventStatus" class="success-text"></div>
            </form>
            <div id="eventList" class="ops-list">
              <div class="subtle">Loading event markers...</div>
            </div>
          </section>

          <section class="ops-card">
            <div class="label">Fault Diagnostics</div>
            <div class="subtle">Windowed fault counts and streaks based on the currently selected chart range.</div>
            <div id="diagnosticsGrid" class="diagnostics-grid">
              <div class="diagnostic-card"><div class="label">Loading</div><div class="value">--</div></div>
            </div>
          </section>
        </section>

        <section>
          <div class="zone-label">Below Chart</div>
          <div class="layout-zone layout-zone-cards" id="belowChartZone" data-zone-id="below-chart"></div>
        </section>

        <section class="rules-panel">
          <div class="chart-top">
            <div>
              <div class="label">Alerting</div>
              <div class="subtle">Manage rules and check whether email, SMS, and push deliveries are succeeding.</div>
            </div>
          </div>

          <div class="tab-strip" role="tablist" aria-label="Alerting tabs">
            <button type="button" class="tab-button active" data-alert-tab="rules" role="tab" aria-selected="true">Rules</button>
            <button type="button" class="tab-button" data-alert-tab="channels" role="tab" aria-selected="false">Channel Setup</button>
            <button type="button" class="tab-button" data-alert-tab="watchdog" role="tab" aria-selected="false">Watchdog</button>
            <button type="button" class="tab-button" data-alert-tab="log" role="tab" aria-selected="false">Message Log</button>
          </div>

          <section id="alertRulesTab" class="tab-panel" data-alert-tab-panel="rules">
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
                    <option value="TIME_ELAPSED">Time Elapsed</option>
                  </select>
                </div>
                <div>
                  <label for="ruleThreshold" id="ruleThresholdLabel">Threshold F</label>
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
                  <label for="ruleHysteresis" id="ruleHysteresisLabel">Reset Gap F</label>
                  <input id="ruleHysteresis" name="hysteresis_f" type="number" step="0.1" value="5" required />
                </div>
                <div>
                  <label for="ruleCooldown">Cooldown Minutes</label>
                  <input id="ruleCooldown" name="notify_cooldown_minutes" type="number" step="0.1" value="15" min="0" required />
                </div>
                <div>
                  <label for="ruleEnabled">Enabled</label>
                  <select id="ruleEnabled" name="enabled">
                    <option value="true" selected>Enabled</option>
                    <option value="false">Disabled</option>
                  </select>
                </div>
                <div>
                  <label for="ruleColor">Accent Color</label>
                  <input id="ruleColor" name="color_hex" class="color-input" type="color" value="#38bdf8" />
                </div>
                <div>
                  <label for="ruleNotifyEmail">Email</label>
                  <select id="ruleNotifyEmail" name="notify_email">
                    <option value="false" selected>Off</option>
                    <option value="true">On</option>
                  </select>
                </div>
                <div>
                  <label for="ruleNotifySms">SMS</label>
                  <select id="ruleNotifySms" name="notify_sms">
                    <option value="false" selected>Off</option>
                    <option value="true">On</option>
                  </select>
                </div>
                <div>
                  <label for="ruleNotifyPush">Push</label>
                  <select id="ruleNotifyPush" name="notify_push">
                    <option value="false" selected>Off</option>
                    <option value="true">On</option>
                  </select>
                </div>
              </div>
              <div class="rule-actions">
                <button type="submit" id="ruleSubmit">Add Rule</button>
                <button type="button" id="ruleCancel">Cancel Edit</button>
              </div>
              <div id="ruleError" class="error-text"></div>
            </form>

            <div class="rules-table-wrap">
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
            </div>
          </section>

          <section id="alertChannelsTab" class="tab-panel" data-alert-tab-panel="channels" hidden>
            <div class="channel-toolbar">
              <button type="button" id="saveChannelSettingsButton">Save Channel Settings</button>
              <button type="button" id="resetChannelSettingsButton">Reload Saved Settings</button>
            </div>
            <div id="channelSettingsStatus" class="success-text"></div>

            <hr class="panel-divider" />

            <div class="channel-health" id="channelHealth">
              <span class="pill">Loading channels...</span>
            </div>

            <hr class="panel-divider" />

            <div class="channel-sections">
              <section class="channel-section">
                <h3 class="channel-section-title">Email</h3>
                <div class="subtle">SMTP settings for alert emails.</div>
                <div class="channel-grid">
                  <div>
                    <label for="emailEnabled">Enabled</label>
                    <select id="emailEnabled">
                      <option value="true">Enabled</option>
                      <option value="false">Disabled</option>
                    </select>
                  </div>
                  <div>
                    <label for="emailSmtpHost">SMTP Host</label>
                    <input id="emailSmtpHost" type="text" placeholder="smtp.example.com" />
                  </div>
                  <div>
                    <label for="emailSmtpPort">SMTP Port</label>
                    <input id="emailSmtpPort" type="number" step="1" placeholder="587" />
                  </div>
                  <div>
                    <label for="emailStarttls">STARTTLS</label>
                    <select id="emailStarttls">
                      <option value="true">Enabled</option>
                      <option value="false">Disabled</option>
                    </select>
                  </div>
                  <div>
                    <label for="emailUsername">Username</label>
                    <input id="emailUsername" type="text" />
                  </div>
                  <div>
                    <label for="emailPassword">Password</label>
                    <input id="emailPassword" type="password" />
                  </div>
                  <div>
                    <label for="emailFromAddr">From</label>
                    <input id="emailFromAddr" type="text" placeholder="kiln@example.com" />
                  </div>
                  <div>
                    <label for="emailToAddr">To</label>
                    <input id="emailToAddr" type="text" placeholder="you@example.com" />
                  </div>
                </div>
              </section>

              <section class="channel-section">
                <h3 class="channel-section-title">SMS</h3>
                <div class="subtle">Twilio settings for text alerts.</div>
                <div class="channel-grid">
                  <div>
                    <label for="smsEnabled">Enabled</label>
                    <select id="smsEnabled">
                      <option value="true">Enabled</option>
                      <option value="false">Disabled</option>
                    </select>
                  </div>
                  <div>
                    <label for="smsAccountSid">Account SID</label>
                    <input id="smsAccountSid" type="text" />
                  </div>
                  <div>
                    <label for="smsAuthToken">Auth Token</label>
                    <input id="smsAuthToken" type="password" />
                  </div>
                  <div>
                    <label for="smsFromNumber">From Number</label>
                    <input id="smsFromNumber" type="text" placeholder="+15551234567" />
                  </div>
                  <div>
                    <label for="smsToNumber">To Number</label>
                    <input id="smsToNumber" type="text" placeholder="+15557654321" />
                  </div>
                </div>
              </section>

              <section class="channel-section">
                <h3 class="channel-section-title">Push</h3>
                <div class="subtle">Generic webhook endpoint for push integrations.</div>
                <div class="channel-grid">
                  <div>
                    <label for="pushEnabled">Enabled</label>
                    <select id="pushEnabled">
                      <option value="true">Enabled</option>
                      <option value="false">Disabled</option>
                    </select>
                  </div>
                  <div>
                    <label for="pushWebhookUrl">Webhook URL</label>
                    <input id="pushWebhookUrl" type="text" placeholder="https://example.com/hook" />
                  </div>
                </div>
              </section>
            </div>
            <div class="subtle test-status" id="testAlertStatus"></div>
          </section>

          <section id="alertWatchdogTab" class="tab-panel" data-alert-tab-panel="watchdog" hidden>
            <div class="channel-toolbar">
              <button type="button" id="saveWatchdogSettingsButton">Save Watchdog Settings</button>
              <button type="button" id="resetWatchdogSettingsButton">Reload Saved Settings</button>
            </div>
            <div id="watchdogSettingsStatus" class="success-text"></div>

            <hr class="panel-divider" />

            <div class="subtle">
              Built-in watchdog alerts cover monitor health conditions such as repeated sensor faults and stale data, even when no user-defined temperature rule has fired.
            </div>
            <div class="subtle">
              Changes here take effect after restarting the `kiln-monitor` service.
            </div>

            <hr class="panel-divider" />

            <div class="watchdog-grid">
              <div>
                <label for="watchdogFaultStreakThreshold">Fault Streak Threshold</label>
                <input id="watchdogFaultStreakThreshold" type="number" min="1" step="1" />
              </div>
              <div>
                <label for="watchdogStaleDataSeconds">Stale Data Seconds</label>
                <input id="watchdogStaleDataSeconds" type="number" min="1" step="1" />
              </div>
              <div>
                <label for="watchdogCooldownMinutes">Cooldown Minutes</label>
                <input id="watchdogCooldownMinutes" type="number" min="0" step="0.1" />
              </div>
            </div>
          </section>

          <section id="alertDeliveriesTab" class="tab-panel" data-alert-tab-panel="log" hidden>
            <div class="tab-panel-header">
              <div>
                <div class="label">Alert Log</div>
                <div class="delivery-summary" id="deliveriesSummary">Checking recent alert activity and delivery attempts...</div>
              </div>
            </div>

            <div class="rules-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Rule</th>
                    <th>Channel</th>
                    <th>Result</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody id="deliveriesTableBody">
                  <tr><td colspan="5" class="subtle">Loading deliveries...</td></tr>
                </tbody>
              </table>
            </div>
          </section>
        </section>

        <section class="profiles-panel">
          <div class="chart-top">
            <div>
              <div class="label">Profiles</div>
              <div class="subtle">Track a planned firing profile now so the monitor can show expected progression before we ever touch control logic.</div>
            </div>
          </div>

          <div id="profileStatusMessage" class="success-text"></div>

          <div class="profile-summary" id="activeProfileSummary">
            <div class="profile-summary-card">
              <div class="label">Tracking</div>
              <div class="value" id="activeProfileName">No active profile</div>
            </div>
            <div class="profile-summary-card">
              <div class="label">Segment</div>
              <div class="value" id="activeProfileSegment">--</div>
            </div>
            <div class="profile-summary-card">
              <div class="label">Expected Temp</div>
              <div class="value" id="activeProfileExpectedTemp">--</div>
            </div>
            <div class="profile-summary-card">
              <div class="label">Elapsed</div>
              <div class="value" id="activeProfileElapsed">--</div>
            </div>
          </div>

          <div class="profile-toolbar">
            <button type="button" id="profileStopButton">Stop Tracking</button>
            <button type="button" id="profileRefreshButton">Reload Profiles</button>
          </div>

          <hr class="panel-divider" />

          <form id="profileForm">
            <div class="profile-grid">
              <div>
                <label for="profileName">Profile Name</label>
                <input id="profileName" type="text" placeholder="Cone 06 glaze" required />
              </div>
              <div>
                <label for="profileCone">Cone</label>
                <input id="profileCone" type="text" placeholder="06" />
              </div>
              <div style="grid-column: 1 / -1;">
                <label for="profileDescription">Description</label>
                <input id="profileDescription" type="text" placeholder="Low-fire glaze schedule with hold at top." />
              </div>
            </div>

            <div class="profile-toolbar">
              <button type="button" id="addSegmentButton">Add Segment</button>
            </div>

            <div class="rules-table-wrap profile-segments-table">
              <table>
                <thead>
                  <tr>
                    <th>Segment</th>
                    <th>Target C</th>
                    <th>Ramp C/hr</th>
                    <th>Soak Min</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody id="profileSegmentsBody">
                  <tr><td colspan="5" class="subtle">Add at least one segment.</td></tr>
                </tbody>
              </table>
            </div>

            <div class="profile-toolbar">
              <button type="submit" id="profileSubmit">Save Profile</button>
              <button type="button" id="profileCancel">Cancel Edit</button>
            </div>
            <div id="profileError" class="error-text"></div>
          </form>

          <hr class="panel-divider" />

          <div class="rules-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Cone</th>
                  <th>Segments</th>
                  <th>Duration</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody id="profilesTableBody">
                <tr><td colspan="5" class="subtle">Loading profiles...</td></tr>
              </tbody>
            </table>
          </div>
        </section>

        <section class="camera-panel">
          <div class="chart-top">
            <div>
              <div class="label">Camera</div>
              <div class="subtle">Take a still snapshot from the kiln area and keep the latest image visible in the dashboard.</div>
            </div>
          </div>

          <div class="camera-shell">
            <div id="cameraPreviewWrap">
              <div id="cameraEmptyState" class="camera-empty">No snapshot captured yet.</div>
              <img id="cameraPreview" class="camera-preview" alt="Latest kiln camera snapshot" hidden />
              <div id="cameraTimestampBadge" class="camera-timestamp" hidden>--</div>
            </div>

            <div>
              <div class="camera-meta">
                <div class="camera-meta-card">
                  <div class="label">Latest Capture</div>
                  <div class="value" id="cameraCapturedAt">--</div>
                </div>
                <div class="camera-meta-card">
                  <div class="label">Image</div>
                  <div class="value" id="cameraImageStatus">No image yet</div>
                </div>
              </div>

              <div class="camera-actions">
                <button type="button" id="captureSnapshotButton">Capture Snapshot</button>
                <button type="button" id="refreshSnapshotButton">Refresh Camera</button>
              </div>
              <div id="cameraStatusMessage" class="success-text"></div>
            </div>
          </div>
        </section>
      </div>

    <div id="cardTemplates" hidden>
      <div class="card" data-card-id="latest-temp">
        <div class="label">Latest Temperature</div>
        <div class="value" id="latestTemp">--</div>
      </div>
      <div class="card" data-card-id="last-update">
        <div class="label">Last Update</div>
        <div class="value" id="lastUpdate">--</div>
      </div>
      <div class="card" data-card-id="sample-age">
        <div class="label">Sample Age</div>
        <div class="value" id="sampleAge">--</div>
      </div>
      <div class="card" data-card-id="last-fault">
        <div class="label">Last Fault</div>
        <div class="value" id="lastFault">--</div>
        <div class="card-actions">
          <button type="button" class="mini-button" id="inlineResetFaultsButton">Reset Faults</button>
        </div>
      </div>
      <div class="card" data-card-id="total-rows">
        <div class="label">Total Rows</div>
        <div class="value" id="totalRows">--</div>
      </div>
      <div class="card" data-card-id="last-alert">
        <div class="label">Last Alert</div>
        <div class="value" id="lastAlert">--</div>
        <div class="card-actions">
          <button type="button" class="mini-button" id="inlineResetAlertsButton">Reset Alerts</button>
        </div>
      </div>
      <div class="card" data-card-id="active-profile">
        <div class="label">Active Profile</div>
        <div class="value" id="activeProfileCard">none</div>
      </div>
    </div>
  </main>

  <script>
    const banner = document.getElementById("statusBanner");
    const latestTemp = document.getElementById("latestTemp");
    const lastUpdate = document.getElementById("lastUpdate");
    const sampleAge = document.getElementById("sampleAge");
    const lastFault = document.getElementById("lastFault");
    const totalRows = document.getElementById("totalRows");
    const lastAlert = document.getElementById("lastAlert");
    const activeProfileCard = document.getElementById("activeProfileCard");
    const chartMeta = document.getElementById("chartMeta");
    const ruleForm = document.getElementById("ruleForm");
    const ruleSubmit = document.getElementById("ruleSubmit");
    const ruleCancel = document.getElementById("ruleCancel");
    const ruleError = document.getElementById("ruleError");
    const rulesTableBody = document.getElementById("rulesTableBody");
    const deliveriesTableBody = document.getElementById("deliveriesTableBody");
    const deliveriesSummary = document.getElementById("deliveriesSummary");
    const channelHealth = document.getElementById("channelHealth");
    const emailEnabled = document.getElementById("emailEnabled");
    const emailSmtpHost = document.getElementById("emailSmtpHost");
    const emailSmtpPort = document.getElementById("emailSmtpPort");
    const emailStarttls = document.getElementById("emailStarttls");
    const emailUsername = document.getElementById("emailUsername");
    const emailPassword = document.getElementById("emailPassword");
    const emailFromAddr = document.getElementById("emailFromAddr");
    const emailToAddr = document.getElementById("emailToAddr");
    const smsEnabled = document.getElementById("smsEnabled");
    const smsAccountSid = document.getElementById("smsAccountSid");
    const smsAuthToken = document.getElementById("smsAuthToken");
    const smsFromNumber = document.getElementById("smsFromNumber");
    const smsToNumber = document.getElementById("smsToNumber");
    const pushEnabled = document.getElementById("pushEnabled");
    const pushWebhookUrl = document.getElementById("pushWebhookUrl");
    const saveChannelSettingsButton = document.getElementById("saveChannelSettingsButton");
    const resetChannelSettingsButton = document.getElementById("resetChannelSettingsButton");
    const channelSettingsStatus = document.getElementById("channelSettingsStatus");
    const testAlertStatus = document.getElementById("testAlertStatus");
    const watchdogFaultStreakThreshold = document.getElementById("watchdogFaultStreakThreshold");
    const watchdogStaleDataSeconds = document.getElementById("watchdogStaleDataSeconds");
    const watchdogCooldownMinutes = document.getElementById("watchdogCooldownMinutes");
    const saveWatchdogSettingsButton = document.getElementById("saveWatchdogSettingsButton");
    const resetWatchdogSettingsButton = document.getElementById("resetWatchdogSettingsButton");
    const watchdogSettingsStatus = document.getElementById("watchdogSettingsStatus");
    const profileForm = document.getElementById("profileForm");
    const profileSubmit = document.getElementById("profileSubmit");
    const profileCancel = document.getElementById("profileCancel");
    const profileError = document.getElementById("profileError");
    const profileSegmentsBody = document.getElementById("profileSegmentsBody");
    const profilesTableBody = document.getElementById("profilesTableBody");
    const addSegmentButton = document.getElementById("addSegmentButton");
    const profileStopButton = document.getElementById("profileStopButton");
    const profileRefreshButton = document.getElementById("profileRefreshButton");
    const profileStatusMessage = document.getElementById("profileStatusMessage");
    const activeProfileName = document.getElementById("activeProfileName");
    const activeProfileSegment = document.getElementById("activeProfileSegment");
    const activeProfileExpectedTemp = document.getElementById("activeProfileExpectedTemp");
    const activeProfileElapsed = document.getElementById("activeProfileElapsed");
    const cameraPreview = document.getElementById("cameraPreview");
    const cameraEmptyState = document.getElementById("cameraEmptyState");
    const cameraTimestampBadge = document.getElementById("cameraTimestampBadge");
    const cameraCapturedAt = document.getElementById("cameraCapturedAt");
    const cameraImageStatus = document.getElementById("cameraImageStatus");
    const cameraStatusMessage = document.getElementById("cameraStatusMessage");
    const captureSnapshotButton = document.getElementById("captureSnapshotButton");
    const refreshSnapshotButton = document.getElementById("refreshSnapshotButton");
    const eventForm = document.getElementById("eventForm");
    const eventStatus = document.getElementById("eventStatus");
    const eventList = document.getElementById("eventList");
    const diagnosticsGrid = document.getElementById("diagnosticsGrid");
    const topSummaryZone = document.getElementById("topSummaryZone");
    const belowChartZone = document.getElementById("belowChartZone");
    const layoutZones = [topSummaryZone, belowChartZone];
    const canvas = document.getElementById("tempChart");
    const ctx = canvas.getContext("2d");
    const resolutionSelect = document.getElementById("resolutionSelect");
    const unitSelect = document.getElementById("unitSelect");
    const layoutToggle = document.getElementById("layoutToggle");
    const openSetupButton = document.getElementById("openSetupButton");
    const closeSetupButton = document.getElementById("closeSetupButton");
    const setupModal = document.getElementById("setupModal");
    const accentColorPicker = document.getElementById("accentColorPicker");
    const pageBgPicker = document.getElementById("pageBgPicker");
    const panelBgPicker = document.getElementById("panelBgPicker");
    const ruleThresholdLabel = document.getElementById("ruleThresholdLabel");
    const ruleHysteresisLabel = document.getElementById("ruleHysteresisLabel");
    const alertTabButtons = Array.from(document.querySelectorAll("[data-alert-tab]"));
    const alertTabPanels = Array.from(document.querySelectorAll("[data-alert-tab-panel]"));
    let selectedRange = "24h";
    let selectedResolution = "auto";
    let selectedUnit = "F";
    let hoverX = null;
    let smoothingEnabled = true;
    let editingRuleId = null;
    let currentAccentColor = "#38bdf8";
    let baseAccentColor = "#38bdf8";
    let layoutEditEnabled = false;
    const HISTORY_BUCKET_PRESETS = {"1h":[2,10,30,60,300],"24h":[60,300,600,900,1800],"7d":[300,600,1800,3600,10800]};
    let currentRules = [];
    let currentDeliveries = [];
    let alertChannelStatus = {};
    let alertChannelSettings = {};
    let watchdogSettings = {};
    let currentProfiles = [];
    let currentProfileRun = null;
    let editingProfileId = null;
    let editingProfileSegments = [];
    let currentCameraStatus = null;
    let currentEvents = [];
    let chartState = {
      points: [],
      plotPoints: [],
      overlayPoints: [],
      events: [],
    };

    function setActiveAlertTab(tabName) {
      alertTabButtons.forEach((button) => {
        const isActive = button.dataset.alertTab === tabName;
        button.classList.toggle("active", isActive);
        button.setAttribute("aria-selected", isActive ? "true" : "false");
      });
      alertTabPanels.forEach((panel) => {
        panel.hidden = panel.dataset.alertTabPanel !== tabName;
      });
    }

    function setSetupModalOpen(isOpen) {
      setupModal.hidden = !isOpen;
    }

    function renderChannelHealth() {
      const channelLabels = {
        EMAIL: "Email",
        SMS: "SMS",
        PUSH: "Push",
      };
      channelHealth.innerHTML = "";
      ["EMAIL", "SMS", "PUSH"].forEach((channel) => {
        const configured = Boolean(alertChannelStatus[channel]);
        const row = document.createElement("div");
        row.className = "channel-status-row";
        const statusClass = configured ? "pill pill-on" : "pill pill-warning";
        const statusText = configured ? "Configured" : "Not Configured";
        row.innerHTML = `
          <div class="channel-status-main">
            <div class="channel-status-name">${channelLabels[channel]}</div>
            <span class="${statusClass}">${statusText}</span>
          </div>
          <button type="button" class="channel-test-button" data-test-channel="${channel}" ${configured ? "" : "disabled"}>Test</button>
        `;
        channelHealth.appendChild(row);
      });

      channelHealth.querySelectorAll("[data-test-channel]").forEach((button) => {
        button.addEventListener("click", async () => {
          await sendTestAlert([button.dataset.testChannel]);
        });
      });
    }

    function populateChannelSettingsForm(settings) {
      alertChannelSettings = settings || {};
      const email = alertChannelSettings.email || {};
      const sms = alertChannelSettings.sms || {};
      const push = alertChannelSettings.push || {};

      emailEnabled.value = String(Boolean(email.enabled));
      emailSmtpHost.value = email.smtp_host || "";
      emailSmtpPort.value = email.smtp_port ?? 587;
      emailStarttls.value = String(Boolean(email.starttls));
      emailUsername.value = email.username || "";
      emailPassword.value = email.password || "";
      emailFromAddr.value = email.from_addr || "";
      emailToAddr.value = email.to_addr || "";

      smsEnabled.value = String(Boolean(sms.enabled));
      smsAccountSid.value = sms.account_sid || "";
      smsAuthToken.value = sms.auth_token || "";
      smsFromNumber.value = sms.from_number || "";
      smsToNumber.value = sms.to_number || "";

      pushEnabled.value = String(Boolean(push.enabled));
      pushWebhookUrl.value = push.webhook_url || "";
    }

    function gatherChannelSettings() {
      return {
        email: {
          enabled: emailEnabled.value === "true",
          smtp_host: emailSmtpHost.value.trim(),
          smtp_port: Number(emailSmtpPort.value || "587"),
          starttls: emailStarttls.value === "true",
          username: emailUsername.value.trim(),
          password: emailPassword.value,
          from_addr: emailFromAddr.value.trim(),
          to_addr: emailToAddr.value.trim(),
        },
        sms: {
          enabled: smsEnabled.value === "true",
          account_sid: smsAccountSid.value.trim(),
          auth_token: smsAuthToken.value,
          from_number: smsFromNumber.value.trim(),
          to_number: smsToNumber.value.trim(),
        },
        push: {
          enabled: pushEnabled.value === "true",
          webhook_url: pushWebhookUrl.value.trim(),
        },
      };
    }

    function populateWatchdogSettingsForm(settings) {
      watchdogSettings = settings || {};
      watchdogFaultStreakThreshold.value = watchdogSettings.fault_streak_threshold ?? 5;
      watchdogStaleDataSeconds.value = watchdogSettings.stale_data_seconds ?? 30;
      watchdogCooldownMinutes.value = watchdogSettings.notify_cooldown_minutes ?? 30;
    }

    function gatherWatchdogSettings() {
      return {
        fault_streak_threshold: Number(watchdogFaultStreakThreshold.value || "5"),
        stale_data_seconds: Number(watchdogStaleDataSeconds.value || "30"),
        notify_cooldown_minutes: Number(watchdogCooldownMinutes.value || "30"),
      };
    }

    function formatTimestamp(isoText) {
      if (!isoText) {
        return "--";
      }
      return new Date(isoText).toLocaleString();
    }

    function formatCompactTimestamp(isoText) {
      if (!isoText) {
        return "--";
      }
      return new Date(isoText).toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
        month: "short",
        day: "numeric",
      });
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
      if (ruleType === "TIME_ELAPSED") {
        return "Elapsed";
      }
      return ruleType;
    }

    function fToC(tempF) {
      return (tempF - 32.0) * 5.0 / 9.0;
    }

    function cToF(tempC) {
      return (tempC * 9.0 / 5.0) + 32.0;
    }

    function unitSuffix() {
      return selectedUnit === "C" ? "C" : "F";
    }

    function displayTempFromStoredF(tempF) {
      if (tempF === null || tempF === undefined || !Number.isFinite(tempF)) {
        return null;
      }
      return selectedUnit === "C" ? fToC(tempF) : tempF;
    }

    function displayDeltaFromStoredF(tempF) {
      if (tempF === null || tempF === undefined || !Number.isFinite(tempF)) {
        return null;
      }
      return selectedUnit === "C" ? (tempF * 5.0 / 9.0) : tempF;
    }

    function formatPrimaryTemperature(tempF, tempC) {
      if (tempF === null || tempC === null || tempF === undefined || tempC === undefined) {
        return "--";
      }
      if (selectedUnit === "C") {
        return `${tempC.toFixed(1)} C`;
      }
      if (selectedUnit === "BOTH") {
        return `${tempF.toFixed(1)} F / ${tempC.toFixed(1)} C`;
      }
      return `${tempF.toFixed(1)} F`;
    }

    function formatRuleTemperatureFromStoredF(tempF) {
      const converted = displayTempFromStoredF(tempF);
      return converted === null ? "--" : `${converted.toFixed(1)} ${unitSuffix()}`;
    }

    function formatRuleDeltaFromStoredF(tempF) {
      const converted = displayDeltaFromStoredF(tempF);
      return converted === null ? "--" : `${converted.toFixed(1)} ${unitSuffix()}`;
    }

    function convertRuleInputToStoredF(value) {
      const numericValue = Number(value);
      if (!Number.isFinite(numericValue)) {
        return numericValue;
      }
      return selectedUnit === "C" ? cToF(numericValue) : numericValue;
    }

    function convertStoredFToRuleInput(value) {
      if (!Number.isFinite(value)) {
        return value;
      }
      return selectedUnit === "C" ? fToC(value) : value;
    }

    function updateRuleUnitLabels() {
      const ruleType = document.getElementById("ruleType").value;
      const suffix = unitSuffix();
      const hysteresisInput = document.getElementById("ruleHysteresis");
      if (ruleType === "TIME_ELAPSED") {
        ruleThresholdLabel.textContent = "Elapsed Minutes";
        ruleHysteresisLabel.textContent = "Reset Gap";
        hysteresisInput.disabled = true;
        hysteresisInput.value = "0";
      } else {
        ruleThresholdLabel.textContent = `Threshold ${suffix}`;
        ruleHysteresisLabel.textContent = `Reset Gap ${suffix}`;
        hysteresisInput.disabled = false;
      }
    }

    function formatDuration(seconds) {
      if (!Number.isFinite(seconds) || seconds <= 0) {
        return "0m";
      }
      const totalMinutes = Math.round(seconds / 60);
      const hours = Math.floor(totalMinutes / 60);
      const minutes = totalMinutes % 60;
      if (hours <= 0) {
        return `${minutes}m`;
      }
      return `${hours}h ${minutes}m`;
    }

    function emptyProfileSegment() {
      return {
        name: "",
        target_temp_c: 999,
        ramp_rate_c_per_hour: 120,
        soak_minutes: 0,
      };
    }

    function resetProfileForm() {
      editingProfileId = null;
      editingProfileSegments = [emptyProfileSegment()];
      document.getElementById("profileName").value = "";
      document.getElementById("profileCone").value = "";
      document.getElementById("profileDescription").value = "";
      profileSubmit.textContent = "Save Profile";
      profileError.textContent = "";
      renderProfileSegmentsEditor();
    }

    function renderProfileSegmentsEditor() {
      if (!editingProfileSegments.length) {
        profileSegmentsBody.innerHTML = '<tr><td colspan="5" class="subtle">Add at least one segment.</td></tr>';
        return;
      }

      profileSegmentsBody.innerHTML = "";
      editingProfileSegments.forEach((segment, index) => {
        const row = document.createElement("tr");
        row.innerHTML = `
          <td><input type="text" data-segment-field="name" data-segment-index="${index}" value="${segment.name || ""}" placeholder="Warmup" /></td>
          <td><input type="number" step="0.1" data-segment-field="target_temp_c" data-segment-index="${index}" value="${Number(segment.target_temp_c ?? 0)}" /></td>
          <td><input type="number" step="0.1" min="0.1" data-segment-field="ramp_rate_c_per_hour" data-segment-index="${index}" value="${Number(segment.ramp_rate_c_per_hour ?? 0)}" /></td>
          <td><input type="number" step="0.1" min="0" data-segment-field="soak_minutes" data-segment-index="${index}" value="${Number(segment.soak_minutes ?? 0)}" /></td>
          <td><button type="button" data-remove-segment="${index}">Remove</button></td>
        `;
        profileSegmentsBody.appendChild(row);
      });

      profileSegmentsBody.querySelectorAll("[data-segment-field]").forEach((input) => {
        input.addEventListener("input", (event) => {
          const target = event.target;
          const index = Number(target.dataset.segmentIndex);
          const field = target.dataset.segmentField;
          const rawValue = target.value;
          editingProfileSegments[index][field] = field === "name" ? rawValue : Number(rawValue);
        });
      });

      profileSegmentsBody.querySelectorAll("[data-remove-segment]").forEach((button) => {
        button.addEventListener("click", () => {
          editingProfileSegments = editingProfileSegments.filter((_, index) => index !== Number(button.dataset.removeSegment));
          if (!editingProfileSegments.length) {
            editingProfileSegments = [emptyProfileSegment()];
          }
          renderProfileSegmentsEditor();
        });
      });
    }

    function gatherProfilePayload() {
      return {
        name: document.getElementById("profileName").value.trim(),
        cone: document.getElementById("profileCone").value.trim(),
        description: document.getElementById("profileDescription").value.trim(),
        segments: editingProfileSegments.map((segment) => ({
          name: String(segment.name || "").trim(),
          target_temp_c: Number(segment.target_temp_c),
          ramp_rate_c_per_hour: Number(segment.ramp_rate_c_per_hour),
          soak_minutes: Number(segment.soak_minutes || 0),
        })),
      };
    }

    function resetRuleForm() {
      editingRuleId = null;
      ruleForm.reset();
      document.getElementById("ruleType").value = "TARGET_REACHED";
      document.getElementById("ruleSeverity").value = "WARNING";
      document.getElementById("ruleEnabled").value = "true";
      document.getElementById("ruleHysteresis").value = "5";
      document.getElementById("ruleCooldown").value = "15";
      document.getElementById("ruleColor").value = "#38bdf8";
      document.getElementById("ruleNotifyEmail").value = "false";
      document.getElementById("ruleNotifySms").value = "false";
      document.getElementById("ruleNotifyPush").value = "false";
      ruleSubmit.textContent = "Add Rule";
      ruleError.textContent = "";
      updateRuleUnitLabels();
    }

    function renderEventList(events) {
      currentEvents = events || [];
      if (!currentEvents.length) {
        eventList.innerHTML = '<div class="subtle">No event markers in this window yet.</div>';
        return;
      }

      eventList.innerHTML = "";
      currentEvents.forEach((event) => {
        const row = document.createElement("div");
        row.className = "ops-item";
        row.innerHTML = `
          <div class="ops-item-top">
            <div class="ops-item-label">${event.label}</div>
            <div class="ops-item-time">${formatTimestamp(event.timestamp_utc)}</div>
          </div>
          <div class="ops-item-detail"><span class="event-badge">${event.event_type}</span> ${event.detail || ""}</div>
        `;
        eventList.appendChild(row);
      });
    }

    function renderDiagnostics(diagnostics) {
      if (!diagnostics) {
        diagnosticsGrid.innerHTML = '<div class="diagnostic-card"><div class="label">No data</div><div class="value">--</div></div>';
        return;
      }

      const detailText = (diagnostics.top_fault_details || []).length
        ? diagnostics.top_fault_details.map((item) => `${item.detail} (${item.count})`).join(", ")
        : "none";
      diagnosticsGrid.innerHTML = `
        <div class="diagnostic-card">
          <div class="label">Fault Samples</div>
          <div class="value">${Number(diagnostics.fault_samples || 0).toLocaleString()}</div>
        </div>
        <div class="diagnostic-card">
          <div class="label">Fault Rate</div>
          <div class="value">${Number(diagnostics.fault_rate_percent || 0).toFixed(1)}%</div>
        </div>
        <div class="diagnostic-card">
          <div class="label">Longest Streak</div>
          <div class="value">${Number(diagnostics.longest_fault_streak || 0)} samples</div>
        </div>
        <div class="diagnostic-card">
          <div class="label">Current Streak</div>
          <div class="value">${Number(diagnostics.current_fault_streak || 0)} samples</div>
        </div>
        <div class="diagnostic-card">
          <div class="label">Top Faults</div>
          <div class="value">${detailText}</div>
        </div>
      `;
    }

    function hexToSoftRgba(hexColor, alpha) {
      const hex = hexColor.replace("#", "");
      const r = parseInt(hex.slice(0, 2), 16);
      const g = parseInt(hex.slice(2, 4), 16);
      const b = parseInt(hex.slice(4, 6), 16);
      return `rgba(${r}, ${g}, ${b}, ${alpha})`;
    }

    function applyAccentColor(hexColor) {
      currentAccentColor = hexColor || "#38bdf8";
      document.documentElement.style.setProperty("--accent-color", currentAccentColor);
      document.documentElement.style.setProperty("--accent-soft", hexToSoftRgba(currentAccentColor, 0.22));
    }

    function applyTheme(theme) {
      const resolvedTheme = {
        accent: theme?.accent || "#38bdf8",
        pageBg: theme?.pageBg || "#0b1220",
        panelBg: theme?.panelBg || "#111827",
      };
      baseAccentColor = resolvedTheme.accent;
      document.documentElement.style.setProperty("--page-bg", resolvedTheme.pageBg);
      document.documentElement.style.setProperty("--page-bg-secondary", resolvedTheme.pageBg);
      document.documentElement.style.setProperty("--panel-bg", resolvedTheme.panelBg);
      document.documentElement.style.setProperty("--panel-border", hexToSoftRgba(resolvedTheme.accent, 0.22));
      applyAccentColor(resolvedTheme.accent);
      accentColorPicker.value = resolvedTheme.accent;
      pageBgPicker.value = resolvedTheme.pageBg;
      panelBgPicker.value = resolvedTheme.panelBg;
    }

    function loadTheme() {
      applyTheme(null);
    }

    async function saveTheme() {
      const theme = {
        accent: accentColorPicker.value,
        pageBg: pageBgPicker.value,
        panelBg: panelBgPicker.value,
      };
      applyTheme(theme);
      await fetch("/api/dashboard-preferences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ theme }),
      });
    }

    async function saveDisplayUnit() {
      await fetch("/api/dashboard-preferences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_unit: selectedUnit }),
      });
    }

    function populateResolutionOptions() {
      const options = HISTORY_BUCKET_PRESETS[selectedRange] || HISTORY_BUCKET_PRESETS["24h"];
      const currentValue = selectedResolution;
      resolutionSelect.innerHTML = '<option value="auto">Auto</option>';
      options.forEach((seconds) => {
        const option = document.createElement("option");
        option.value = String(seconds);
        option.textContent = seconds < 60
          ? `${seconds}s`
          : seconds < 3600
            ? `${Math.round(seconds / 60)}m`
            : `${Math.round(seconds / 3600)}h`;
        resolutionSelect.appendChild(option);
      });
      if (currentValue === "auto" || options.includes(Number(currentValue))) {
        resolutionSelect.value = currentValue;
      } else {
        selectedResolution = "auto";
        resolutionSelect.value = "auto";
      }
    }

    function defaultCardLayout() {
      return {
        "top-summary": ["latest-temp", "last-update", "sample-age", "total-rows"],
        "below-chart": ["last-fault", "last-alert", "active-profile"],
      };
    }

    function normalizeCardLayout(layout) {
      const normalized = defaultCardLayout();
      if (!layout || typeof layout !== "object" || Array.isArray(layout)) {
        return normalized;
      }

      const topSummary = Array.isArray(layout["top-summary"]) ? layout["top-summary"] : [];
      const belowChart = Array.isArray(layout["below-chart"]) ? layout["below-chart"] : [];
      const legacySidebar = Array.isArray(layout["sidebar"]) ? layout["sidebar"] : [];

      normalized["top-summary"] = [...topSummary];
      normalized["below-chart"] = [...belowChart, ...legacySidebar];

      const seen = new Set();
      Object.keys(normalized).forEach((zoneId) => {
        normalized[zoneId] = normalized[zoneId].filter((cardId) => {
          if (seen.has(cardId)) {
            return false;
          }
          seen.add(cardId);
          return true;
        });
      });

      return normalized;
    }

    function getAllCards() {
      return Array.from(document.querySelectorAll(".card[data-card-id]"));
    }

    function applyCardLayout(layout) {
      const normalizedLayout = normalizeCardLayout(layout);
      const cards = getAllCards();
      const cardById = new Map(cards.map((card) => [card.dataset.cardId, card]));
      layoutZones.forEach((zone) => {
        zone.innerHTML = "";
      });
      ["top-summary", "below-chart"].forEach((zoneId) => {
        const zone = layoutZones.find((item) => item.dataset.zoneId === zoneId);
        const cardIds = normalizedLayout[zoneId] || [];
        cardIds.forEach((cardId) => {
          const card = cardById.get(cardId);
          if (card && zone) {
            zone.appendChild(card);
            cardById.delete(cardId);
          }
        });
      });
      cardById.forEach((card) => topSummaryZone.appendChild(card));
    }

    function currentCardLayout() {
      const layout = {};
      layoutZones.forEach((zone) => {
        layout[zone.dataset.zoneId] = Array.from(zone.querySelectorAll(".card")).map((card) => card.dataset.cardId);
      });
      return layout;
    }

    async function saveCardOrder() {
      await fetch("/api/dashboard-preferences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ card_order: currentCardLayout() }),
      });
    }

    function setLayoutEditEnabled(enabled) {
      layoutEditEnabled = enabled;
      layoutToggle.classList.toggle("active", enabled);
      layoutToggle.textContent = enabled ? "Done Editing" : "Edit Layout";
      getAllCards().forEach((card) => {
        card.draggable = enabled;
        card.classList.toggle("layout-edit", enabled);
      });
    }

    function setupCardDragAndDrop() {
      let draggedCard = null;
      getAllCards().forEach((card) => {
        card.addEventListener("dragstart", () => {
          if (!layoutEditEnabled) {
            return;
          }
          draggedCard = card;
          card.classList.add("dragging");
        });
        card.addEventListener("dragend", () => {
          card.classList.remove("dragging");
          getAllCards().forEach((item) => item.classList.remove("drag-target"));
          layoutZones.forEach((zone) => zone.classList.remove("drag-target-zone"));
          if (draggedCard) {
            void saveCardOrder();
          }
          draggedCard = null;
        });
        card.addEventListener("dragover", (event) => {
          if (!layoutEditEnabled || !draggedCard || draggedCard === card) {
            return;
          }
          event.preventDefault();
          getAllCards().forEach((item) => item.classList.remove("drag-target"));
          card.classList.add("drag-target");
          const rect = card.getBoundingClientRect();
          const before = event.clientY < rect.top + rect.height / 2;
          if (before) {
            card.parentElement.insertBefore(draggedCard, card);
          } else {
            card.parentElement.insertBefore(draggedCard, card.nextSibling);
          }
        });
        card.addEventListener("dragleave", () => {
          card.classList.remove("drag-target");
        });
      });

      layoutZones.forEach((zone) => {
        zone.addEventListener("dragover", (event) => {
          if (!layoutEditEnabled || !draggedCard) {
            return;
          }
          event.preventDefault();
          zone.classList.add("drag-target-zone");
          if (!zone.querySelector(".card")) {
            zone.appendChild(draggedCard);
          }
        });
        zone.addEventListener("dragleave", () => {
          zone.classList.remove("drag-target-zone");
        });
        zone.addEventListener("drop", (event) => {
          if (!layoutEditEnabled || !draggedCard) {
            return;
          }
          event.preventDefault();
          zone.classList.remove("drag-target-zone");
          zone.appendChild(draggedCard);
        });
      });
    }

    async function postReset(path) {
      const response = await fetch(path, { method: "POST" });
      if (!response.ok) {
        const payload = await response.json();
        throw new Error(payload.error || "Reset failed");
      }
      await refreshAll();
    }

    async function loadDashboardPreferences() {
      const response = await fetch("/api/dashboard-preferences");
      const payload = await response.json();
      if (payload.theme) {
        applyTheme(payload.theme);
      } else {
        applyTheme(null);
      }
      if (payload.display_unit && ["F", "C", "BOTH"].includes(payload.display_unit)) {
        selectedUnit = payload.display_unit;
      }
      unitSelect.value = selectedUnit;
      updateRuleUnitLabels();
      if (payload.card_order && typeof payload.card_order === "object" && !Array.isArray(payload.card_order)) {
        applyCardLayout(payload.card_order);
      } else {
        applyCardLayout(defaultCardLayout());
      }
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
        ? formatPrimaryTemperature(nearest.temp_f, nearest.temp_c)
        : `ERROR: ${nearest.detail || "fault"}`;
      ctx.fillText(valueText, boxX + 12, boxY + 23);

      ctx.fillStyle = "#cbd5e1";
      ctx.font = "12px system-ui, sans-serif";
      ctx.fillText(formatTimestamp(nearest.timestamp_utc), boxX + 12, boxY + 42);
    }

    function drawChart(points, overlayPoints = [], events = []) {
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
        overlayPoints,
        events,
        top,
        plotHeight,
      };

      ctx.fillStyle = "#0f172a";
      ctx.fillRect(0, 0, width, height);
      ctx.strokeStyle = "#334155";
      ctx.lineWidth = 1;
      ctx.strokeRect(left, top, plotWidth, plotHeight);

      if (!displayPoints.length && !overlayPoints.length) {
        ctx.fillStyle = "#9ca3af";
        ctx.font = "14px system-ui, sans-serif";
        ctx.fillText("No samples in this window yet.", left + 12, top + 28);
        chartMeta.textContent = "No samples available.";
        return;
      }

      const timeSourcePoints = displayPoints.length ? displayPoints : overlayPoints;
      const times = timeSourcePoints.map((point) => new Date(point.timestamp_utc).getTime());
      const validTemps = displayPoints
        .map((point) => displayTempFromStoredF(point.temp_f))
        .filter((temp) => temp !== null && Number.isFinite(temp));
      const overlayTemps = overlayPoints
        .map((point) => displayTempFromStoredF(point.temp_f))
        .filter((temp) => temp !== null && Number.isFinite(temp));
      const minTime = Math.min(...times);
      const maxTime = Math.max(...times);
      const combinedTemps = [...validTemps, ...overlayTemps];
      const minTemp = combinedTemps.length ? Math.min(...combinedTemps) : 0;
      const maxTemp = combinedTemps.length ? Math.max(...combinedTemps) : 1;
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

      function yFor(displayTemp) {
        return top + plotHeight - ((displayTemp - paddedMinTemp) / tempSpan) * plotHeight;
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
        ctx.fillText(`${tempLabel.toFixed(0)} ${unitSuffix()}`, 8, y + 4);
      }

      let segmentOpen = false;
      ctx.strokeStyle = currentAccentColor;
      ctx.lineWidth = 2;
      ctx.beginPath();

      displayPoints.forEach((point) => {
        const pointTime = new Date(point.timestamp_utc).getTime();
        const displayTemp = displayTempFromStoredF(point.temp_f);
        if (displayTemp === null || !Number.isFinite(displayTemp) || point.status !== "OK") {
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
        const y = yFor(displayTemp);
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

      if (overlayPoints.length) {
        ctx.save();
        ctx.strokeStyle = "rgba(248, 250, 252, 0.75)";
        ctx.lineWidth = 2;
        ctx.setLineDash([8, 6]);
        ctx.beginPath();
        let overlayOpen = false;
        overlayPoints.forEach((point) => {
          const displayTemp = displayTempFromStoredF(point.temp_f);
          if (displayTemp === null || !Number.isFinite(displayTemp)) {
            overlayOpen = false;
            return;
          }
          const x = xFor(new Date(point.timestamp_utc).getTime());
          const y = yFor(displayTemp);
          if (!overlayOpen) {
            ctx.moveTo(x, y);
            overlayOpen = true;
          } else {
            ctx.lineTo(x, y);
          }
        });
        ctx.stroke();
        ctx.restore();
      }

      if (events.length) {
        ctx.save();
        events.forEach((event) => {
          const x = xFor(new Date(event.timestamp_utc).getTime());
          ctx.strokeStyle = "rgba(250, 204, 21, 0.78)";
          ctx.lineWidth = 1;
          ctx.setLineDash([4, 6]);
          ctx.beginPath();
          ctx.moveTo(x, top);
          ctx.lineTo(x, top + plotHeight);
          ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = "#facc15";
          ctx.beginPath();
          ctx.arc(x, top + 10, 4, 0, Math.PI * 2);
          ctx.fill();
        });
        ctx.restore();
      }

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
      ctx.fillText(formatTimestamp(timeSourcePoints[0].timestamp_utc), left, height - 12);
      const endLabel = formatTimestamp(timeSourcePoints[timeSourcePoints.length - 1].timestamp_utc);
      const endWidth = ctx.measureText(endLabel).width;
      ctx.fillText(endLabel, left + plotWidth - endWidth, height - 12);

      if (combinedTemps.length) {
        const modeLabel = smoothingEnabled ? "smoothed" : "raw";
        chartMeta.textContent = `${displayPoints.length} samples, ${minTemp.toFixed(1)} ${unitSuffix()} to ${maxTemp.toFixed(1)} ${unitSuffix()}, ${modeLabel}`;
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
        applyAccentColor(payload.active_alert_rule ? payload.active_alert_rule.color_hex : baseAccentColor);
        latestTemp.textContent = "--";
        lastUpdate.textContent = "--";
        sampleAge.textContent = "--";
        lastFault.textContent = payload.latest_fault ? payload.latest_fault.detail : "none";
        lastAlert.textContent = payload.latest_alert ? payload.latest_alert.detail : "none";
        activeProfileCard.textContent = payload.active_profile_run ? payload.active_profile_run.name : "none";
        return;
      }

      const latest = payload.latest_sample;
      const isOk = latest.status === "OK";
      applyAccentColor(payload.active_alert_rule ? payload.active_alert_rule.color_hex : baseAccentColor);
      banner.textContent = isOk ? "Sensor OK" : `Sensor ERROR: ${latest.detail || "fault sample logged"}`;
      banner.className = `status-banner ${isOk ? "status-ok" : "status-error"}`;
      latestTemp.textContent = latest.temp_f === null ? "--" : formatPrimaryTemperature(latest.temp_f, latest.temp_c);
      lastUpdate.textContent = formatTimestamp(latest.timestamp_utc);
      sampleAge.textContent = latest.sample_age;
      lastFault.textContent = payload.latest_fault
        ? `${payload.latest_fault.sample_age} ago: ${payload.latest_fault.detail || "fault"}`
        : "none";
      lastAlert.textContent = payload.latest_alert
        ? `${payload.latest_alert.level}: ${payload.latest_alert.rule_name || payload.latest_alert.kind.toLowerCase()}`
        : "none";
      activeProfileCard.textContent = payload.active_profile_run
        ? `${payload.active_profile_run.name} (${payload.active_profile_run.phase})`
        : "none";
    }

    function renderActiveProfileSummary(run) {
      currentProfileRun = run || null;
      if (!run) {
        activeProfileName.textContent = "No active profile";
        activeProfileSegment.textContent = "--";
        activeProfileExpectedTemp.textContent = "--";
        activeProfileElapsed.textContent = "--";
        profileStopButton.disabled = true;
        return;
      }

      activeProfileName.textContent = run.name || "Tracked Profile";
      activeProfileSegment.textContent = `${run.segment_name || "--"} (${run.phase || "--"})`;
      activeProfileExpectedTemp.textContent = formatPrimaryTemperature(run.expected_temp_f, run.expected_temp_c);
      activeProfileElapsed.textContent = formatDuration(run.elapsed_seconds || 0);
      profileStopButton.disabled = false;
    }

    async function refreshProfiles() {
      const response = await fetch("/api/profiles");
      const payload = await response.json();
      currentProfiles = payload.profiles || [];
      renderActiveProfileSummary(payload.active_run || null);

      if (!currentProfiles.length) {
        profilesTableBody.innerHTML = '<tr><td colspan="5" class="subtle">No firing profiles created yet.</td></tr>';
        return;
      }

      profilesTableBody.innerHTML = "";
      currentProfiles.forEach((profile) => {
        const isActive = currentProfileRun && currentProfileRun.profile_id === profile.id;
        const row = document.createElement("tr");
        row.innerHTML = `
          <td>${profile.name}<div class="subtle">${profile.description || "no description"}</div></td>
          <td>${profile.cone || "--"}</td>
          <td>${profile.segments.length}</td>
          <td>${formatDuration(profile.total_duration_seconds || 0)}</td>
          <td>
            <div class="profile-list-actions">
              <button type="button" data-profile-activate="${profile.id}">${isActive ? "Restart Tracking" : "Track"}</button>
              <button type="button" data-profile-edit="${profile.id}">Edit</button>
              <button type="button" data-profile-delete="${profile.id}">Delete</button>
            </div>
          </td>
        `;
        profilesTableBody.appendChild(row);
      });

      profilesTableBody.querySelectorAll("[data-profile-activate]").forEach((button) => {
        button.addEventListener("click", async () => {
          profileStatusMessage.textContent = "Starting profile tracking...";
          const response = await fetch(`/api/profiles/${button.dataset.profileActivate}/activate`, { method: "POST" });
          const payload = await response.json();
          if (!response.ok) {
            profileStatusMessage.textContent = payload.error || "Failed to start profile tracking.";
            return;
          }
          profileStatusMessage.textContent = payload.message || "Profile tracking started.";
          await Promise.all([refreshProfiles(), refreshStatus(), refreshHistory()]);
        });
      });

      profilesTableBody.querySelectorAll("[data-profile-edit]").forEach((button) => {
        button.addEventListener("click", () => {
          const profile = currentProfiles.find((item) => item.id === Number(button.dataset.profileEdit));
          if (!profile) {
            return;
          }
          editingProfileId = profile.id;
          document.getElementById("profileName").value = profile.name;
          document.getElementById("profileCone").value = profile.cone || "";
          document.getElementById("profileDescription").value = profile.description || "";
          editingProfileSegments = (profile.segments || []).map((segment) => ({
            name: segment.name,
            target_temp_c: Number(segment.target_temp_c),
            ramp_rate_c_per_hour: Number(segment.ramp_rate_c_per_hour),
            soak_minutes: Number(segment.soak_minutes),
          }));
          profileSubmit.textContent = "Save Profile";
          profileError.textContent = "";
          renderProfileSegmentsEditor();
        });
      });

      profilesTableBody.querySelectorAll("[data-profile-delete]").forEach((button) => {
        button.addEventListener("click", async () => {
          const response = await fetch(`/api/profiles/${button.dataset.profileDelete}/delete`, { method: "POST" });
          const payload = await response.json();
          if (!response.ok) {
            profileStatusMessage.textContent = payload.error || "Failed to delete profile.";
            return;
          }
          if (editingProfileId === Number(button.dataset.profileDelete)) {
            resetProfileForm();
          }
          profileStatusMessage.textContent = "Profile deleted.";
          await Promise.all([refreshProfiles(), refreshStatus(), refreshHistory()]);
        });
      });
    }

    function renderCameraStatus(payload) {
      currentCameraStatus = payload || {};
      if (!payload || !payload.available || !payload.latest_url) {
        cameraPreview.hidden = true;
        cameraPreview.removeAttribute("src");
        cameraEmptyState.hidden = false;
        cameraTimestampBadge.hidden = true;
        cameraCapturedAt.textContent = "--";
        cameraImageStatus.textContent = payload?.error || "No image yet";
        return;
      }

      cameraPreview.src = payload.latest_url;
      cameraPreview.hidden = false;
      cameraEmptyState.hidden = true;
      cameraTimestampBadge.hidden = false;
      cameraTimestampBadge.textContent = formatCompactTimestamp(payload.captured_at);
      cameraCapturedAt.textContent = formatTimestamp(payload.captured_at);
      cameraImageStatus.textContent = payload.archived_filename || "latest.jpg";
    }

    async function refreshCameraStatus() {
      const response = await fetch("/api/camera/status");
      const payload = await response.json();
      renderCameraStatus(payload);
    }

    async function captureSnapshot() {
      cameraStatusMessage.textContent = "Capturing snapshot...";
      captureSnapshotButton.disabled = true;
      try {
        const response = await fetch("/api/camera/capture", { method: "POST" });
        const payload = await response.json();
        if (!response.ok) {
          cameraStatusMessage.textContent = payload.error || "Failed to capture snapshot.";
          return;
        }
        cameraStatusMessage.textContent = payload.message || "Snapshot captured.";
        renderCameraStatus(payload.camera || null);
      } finally {
        captureSnapshotButton.disabled = false;
      }
    }

    async function refreshAlertRules() {
      const response = await fetch("/api/alert-rules");
      const payload = await response.json();
      const rules = payload.rules || [];
      currentRules = rules;

      if (!rules.length) {
        rulesTableBody.innerHTML = '<tr><td colspan="7" class="subtle">No alert rules configured yet.</td></tr>';
        return;
      }

      rulesTableBody.innerHTML = "";
      rules.forEach((rule) => {
        const row = document.createElement("tr");
        row.innerHTML = `
          <td><span class="color-swatch" style="background:${rule.color_hex};"></span>${rule.name}<div class="subtle">reset gap ${formatRuleDeltaFromStoredF(rule.hysteresis_f)}</div><div class="subtle">cooldown ${Number(rule.notify_cooldown_minutes ?? 15).toFixed(1)} min</div><div class="subtle">channels: ${[rule.notify_email ? "email" : null, rule.notify_sms ? "sms" : null, rule.notify_push ? "push" : null].filter(Boolean).join(", ") || "none"}</div></td>
          <td>${humanizeRuleType(rule.rule_type)}</td>
          <td>${rule.rule_type === "TIME_ELAPSED" ? `${Number(rule.trigger_minutes || 0).toFixed(1)} min` : formatRuleTemperatureFromStoredF(rule.threshold_f)}</td>
          <td>${rule.severity}</td>
          <td>
            <span class="pill ${rule.enabled ? "pill-on" : "pill-off"}">${rule.enabled ? "Enabled" : "Disabled"}</span>
            ${rule.active ? '<span class="pill pill-active">Active</span>' : ""}
          </td>
          <td>${formatTimestamp(rule.last_triggered_at)}</td>
          <td>
            <button type="button" data-edit="${rule.id}">Edit</button>
            <button type="button" data-clone="${rule.id}">Clone</button>
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
          document.getElementById("ruleThreshold").value = rule.rule_type === "TIME_ELAPSED"
            ? Number(rule.trigger_minutes || 0).toFixed(1)
            : convertStoredFToRuleInput(rule.threshold_f).toFixed(1);
          document.getElementById("ruleSeverity").value = rule.severity;
          document.getElementById("ruleHysteresis").value = rule.rule_type === "TIME_ELAPSED"
            ? "0"
            : convertStoredFToRuleInput(rule.hysteresis_f).toFixed(1);
          document.getElementById("ruleCooldown").value = Number(rule.notify_cooldown_minutes ?? 15).toFixed(1);
          document.getElementById("ruleEnabled").value = rule.enabled ? "true" : "false";
          document.getElementById("ruleColor").value = rule.color_hex;
          document.getElementById("ruleNotifyEmail").value = rule.notify_email ? "true" : "false";
          document.getElementById("ruleNotifySms").value = rule.notify_sms ? "true" : "false";
          document.getElementById("ruleNotifyPush").value = rule.notify_push ? "true" : "false";
          ruleSubmit.textContent = "Save Rule";
          ruleError.textContent = "";
          updateRuleUnitLabels();
        });
      });

      rulesTableBody.querySelectorAll("[data-clone]").forEach((button) => {
        button.addEventListener("click", async () => {
          const response = await fetch(`/api/alert-rules/${button.dataset.clone}/clone`, { method: "POST" });
          const result = await response.json();
          if (!response.ok) {
            ruleError.textContent = result.error || "Failed to clone alert rule.";
            return;
          }
          await refreshAlertRules();
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

    async function refreshAlertDeliveries() {
      const response = await fetch("/api/alert-deliveries");
      const payload = await response.json();
      const deliveries = payload.deliveries || [];
      currentDeliveries = deliveries;

      deliveriesSummary.textContent = deliveries.length
        ? `${deliveries.length} recent alert log entries. Use this tab to confirm alert sends are actually making it out.`
        : "No alert log entries have been recorded yet.";

      if (!deliveries.length) {
        deliveriesTableBody.innerHTML = '<tr><td colspan="5" class="subtle">No alert log entries recorded yet.</td></tr>';
        return;
      }

      deliveriesTableBody.innerHTML = "";
      deliveries.forEach((delivery) => {
        const row = document.createElement("tr");
        const resultClass = delivery.success ? "pill pill-on" : "pill pill-active";
        const resultLabel = delivery.success ? "Sent" : "Failed";
        row.innerHTML = `
          <td>${formatTimestamp(delivery.timestamp_utc)}<div class="subtle">${delivery.sample_age}</div></td>
          <td>${delivery.rule_name || "unknown"}</td>
          <td><div class="channel-badges"><span class="channel-badge">${delivery.channel}</span></div></td>
          <td><span class="${resultClass}">${resultLabel}</span></td>
          <td class="delivery-detail">${delivery.detail || "no detail"}</td>
        `;
        deliveriesTableBody.appendChild(row);
      });
    }

    async function refreshAlertChannels() {
      const response = await fetch("/api/alert-channels");
      const payload = await response.json();
      alertChannelStatus = payload.channels || {};
      renderChannelHealth();
    }

    async function refreshAlertChannelSettings() {
      const response = await fetch("/api/alert-channel-settings");
      const payload = await response.json();
      populateChannelSettingsForm(payload.settings || {});
    }

    async function saveAlertChannelSettings() {
      channelSettingsStatus.textContent = "Saving channel settings...";
      const response = await fetch("/api/alert-channel-settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: gatherChannelSettings() }),
      });
      const payload = await response.json();
      if (!response.ok) {
        channelSettingsStatus.textContent = payload.error || "Failed to save channel settings.";
        return;
      }

      populateChannelSettingsForm(payload.settings || {});
      channelSettingsStatus.textContent = "Channel settings saved.";
      await refreshAlertChannels();
    }

    async function refreshWatchdogSettings() {
      const response = await fetch("/api/watchdog-settings");
      const payload = await response.json();
      populateWatchdogSettingsForm(payload.settings || {});
    }

    async function saveWatchdogSettings() {
      watchdogSettingsStatus.textContent = "Saving watchdog settings...";
      const response = await fetch("/api/watchdog-settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: gatherWatchdogSettings() }),
      });
      const payload = await response.json();
      if (!response.ok) {
        watchdogSettingsStatus.textContent = payload.error || "Failed to save watchdog settings.";
        return;
      }

      populateWatchdogSettingsForm(payload.settings || {});
      watchdogSettingsStatus.textContent = "Watchdog settings saved.";
    }

    async function sendTestAlert(channels) {
      testAlertStatus.textContent = "Sending test alert...";
      const response = await fetch("/api/test-alert", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channels }),
      });
      const payload = await response.json();
      if (!response.ok) {
        testAlertStatus.textContent = payload.error || "Failed to send test alert.";
        return;
      }

      const sentCount = (payload.results || []).filter((result) => result.success).length;
      const totalCount = (payload.results || []).length;
      testAlertStatus.textContent = payload.message || `Sent ${sentCount} of ${totalCount} test alerts.`;
      await refreshAlertDeliveries();
    }

    async function refreshHistory() {
      const params = new URLSearchParams({ range: selectedRange, resolution: selectedResolution });
      const response = await fetch(`/api/history?${params.toString()}`);
      const payload = await response.json();
      drawChart(payload.samples, payload.profile_overlay || [], payload.events || []);
      renderEventList(payload.events || []);
      renderDiagnostics(payload.diagnostics || null);
      if (payload.meta) {
        const resolutionLabel = payload.meta.bucket_seconds >= 3600
          ? `${Math.round(payload.meta.bucket_seconds / 3600)}h`
          : payload.meta.bucket_seconds >= 60
            ? `${Math.round(payload.meta.bucket_seconds / 60)}m`
            : `${payload.meta.bucket_seconds}s`;
        const overlayLabel = (payload.profile_overlay || []).length ? ", profile overlay on" : "";
        chartMeta.textContent = `${payload.meta.returned_samples} plotted from ${payload.meta.raw_rows} raw rows at ${resolutionLabel} buckets${overlayLabel}`;
      }
    }

    async function refreshAll() {
      try {
        await Promise.all([
          refreshStatus(),
          refreshHistory(),
          refreshProfiles(),
          refreshCameraStatus(),
          refreshAlertRules(),
          refreshAlertDeliveries(),
          refreshAlertChannels(),
        ]);
      } catch (error) {
        banner.textContent = `Dashboard refresh failed: ${error}`;
        banner.className = "status-banner status-error";
      }
    }

    document.querySelectorAll("button[data-range]").forEach((button) => {
      button.addEventListener("click", async () => {
        selectedRange = button.dataset.range;
        populateResolutionOptions();
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
      drawChart(chartState.points, chartState.overlayPoints || [], chartState.events || []);
    });

    resolutionSelect.addEventListener("change", async () => {
      selectedResolution = resolutionSelect.value;
      await refreshHistory();
    });

    alertTabButtons.forEach((button) => {
      button.addEventListener("click", () => {
        setActiveAlertTab(button.dataset.alertTab);
      });
    });

    saveChannelSettingsButton.addEventListener("click", async () => {
      await saveAlertChannelSettings();
    });

    resetChannelSettingsButton.addEventListener("click", async () => {
      channelSettingsStatus.textContent = "Reloading saved settings...";
      await refreshAlertChannelSettings();
      channelSettingsStatus.textContent = "Saved settings reloaded.";
    });

    saveWatchdogSettingsButton.addEventListener("click", async () => {
      await saveWatchdogSettings();
    });

    resetWatchdogSettingsButton.addEventListener("click", async () => {
      watchdogSettingsStatus.textContent = "Reloading saved settings...";
      await refreshWatchdogSettings();
      watchdogSettingsStatus.textContent = "Saved settings reloaded.";
    });

    unitSelect.addEventListener("change", async () => {
      selectedUnit = unitSelect.value;
      updateRuleUnitLabels();
      if (editingRuleId !== null) {
        const rule = currentRules.find((item) => item.id === editingRuleId);
        if (rule) {
          if (rule.rule_type === "TIME_ELAPSED") {
            document.getElementById("ruleThreshold").value = Number(rule.trigger_minutes || 0).toFixed(1);
            document.getElementById("ruleHysteresis").value = "0";
          } else {
            document.getElementById("ruleThreshold").value = convertStoredFToRuleInput(rule.threshold_f).toFixed(1);
            document.getElementById("ruleHysteresis").value = convertStoredFToRuleInput(rule.hysteresis_f).toFixed(1);
          }
        }
      }
      await saveDisplayUnit();
      await refreshAll();
    });

    layoutToggle.addEventListener("click", () => {
      if (!setupModal.hidden) {
        setSetupModalOpen(false);
      }
      setLayoutEditEnabled(!layoutEditEnabled);
    });

    openSetupButton.addEventListener("click", () => {
      setSetupModalOpen(true);
    });

    closeSetupButton.addEventListener("click", () => {
      setSetupModalOpen(false);
    });

    setupModal.addEventListener("click", (event) => {
      if (event.target === setupModal) {
        setSetupModalOpen(false);
      }
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !setupModal.hidden) {
        setSetupModalOpen(false);
      }
    });

    accentColorPicker.addEventListener("input", () => { void saveTheme(); });
    pageBgPicker.addEventListener("input", () => { void saveTheme(); });
    panelBgPicker.addEventListener("input", () => { void saveTheme(); });

    document.getElementById("resetColorsButton").addEventListener("click", async () => {
      applyTheme(null);
      await fetch("/api/dashboard-preferences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          theme: {
            accent: "#38bdf8",
            pageBg: "#0b1220",
            panelBg: "#111827",
          },
        }),
      });
    });

    document.getElementById("resetFaultsButton").addEventListener("click", async () => postReset("/api/reset-faults"));
    document.getElementById("resetAlertsButton").addEventListener("click", async () => postReset("/api/reset-alerts"));
    document.getElementById("inlineResetFaultsButton").addEventListener("click", async () => postReset("/api/reset-faults"));
    document.getElementById("inlineResetAlertsButton").addEventListener("click", async () => postReset("/api/reset-alerts"));

    ruleForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      ruleError.textContent = "";
      const ruleType = document.getElementById("ruleType").value;
      const payload = {
        name: document.getElementById("ruleName").value.trim(),
        rule_type: ruleType,
        threshold_f: ruleType === "TIME_ELAPSED" ? 0 : convertRuleInputToStoredF(document.getElementById("ruleThreshold").value),
        trigger_minutes: ruleType === "TIME_ELAPSED" ? Number(document.getElementById("ruleThreshold").value) : null,
        severity: document.getElementById("ruleSeverity").value,
        hysteresis_f: ruleType === "TIME_ELAPSED" ? 0 : convertRuleInputToStoredF(document.getElementById("ruleHysteresis").value),
        notify_cooldown_minutes: Number(document.getElementById("ruleCooldown").value),
        enabled: document.getElementById("ruleEnabled").value === "true",
        color_hex: document.getElementById("ruleColor").value,
        notify_email: document.getElementById("ruleNotifyEmail").value === "true",
        notify_sms: document.getElementById("ruleNotifySms").value === "true",
        notify_push: document.getElementById("ruleNotifyPush").value === "true",
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

    document.getElementById("ruleType").addEventListener("change", () => {
      updateRuleUnitLabels();
    });

    eventForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      eventStatus.textContent = "Saving marker...";
      const response = await fetch("/api/events", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          event_type: document.getElementById("eventType").value,
          label: document.getElementById("eventLabel").value.trim(),
          detail: document.getElementById("eventDetail").value.trim(),
        }),
      });
      const result = await response.json();
      if (!response.ok) {
        eventStatus.textContent = result.error || "Failed to add event marker.";
        return;
      }
      eventForm.reset();
      document.getElementById("eventType").value = "OPERATION";
      eventStatus.textContent = "Event marker added.";
      await refreshHistory();
    });

    profileForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      profileError.textContent = "";
      const payload = gatherProfilePayload();
      const path = editingProfileId === null
        ? "/api/profiles"
        : `/api/profiles/${editingProfileId}`;
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok) {
        profileError.textContent = result.error || "Failed to save profile.";
        return;
      }
      profileStatusMessage.textContent = editingProfileId === null ? "Profile created." : "Profile updated.";
      resetProfileForm();
      await refreshProfiles();
    });

    profileCancel.addEventListener("click", () => {
      resetProfileForm();
    });

    addSegmentButton.addEventListener("click", () => {
      editingProfileSegments.push(emptyProfileSegment());
      renderProfileSegmentsEditor();
    });

    profileStopButton.addEventListener("click", async () => {
      const response = await fetch("/api/profiles/stop", { method: "POST" });
      const payload = await response.json();
      if (!response.ok) {
        profileStatusMessage.textContent = payload.error || "Failed to stop profile tracking.";
        return;
      }
      profileStatusMessage.textContent = payload.message || "Profile tracking stopped.";
      await Promise.all([refreshProfiles(), refreshStatus(), refreshHistory()]);
    });

    profileRefreshButton.addEventListener("click", async () => {
      profileStatusMessage.textContent = "Reloading profiles...";
      await refreshProfiles();
      profileStatusMessage.textContent = "Profiles reloaded.";
    });

    captureSnapshotButton.addEventListener("click", async () => {
      await captureSnapshot();
    });

    refreshSnapshotButton.addEventListener("click", async () => {
      cameraStatusMessage.textContent = "Refreshing camera status...";
      await refreshCameraStatus();
      cameraStatusMessage.textContent = "Camera status refreshed.";
    });

    canvas.addEventListener("mousemove", (event) => {
      hoverX = event.clientX - canvas.getBoundingClientRect().left;
      drawChart(chartState.points, chartState.overlayPoints || [], chartState.events || []);
    });

    canvas.addEventListener("mouseleave", () => {
      hoverX = null;
      drawChart(chartState.points, chartState.overlayPoints || [], chartState.events || []);
    });

    setupCardDragAndDrop();
    populateResolutionOptions();
    window.addEventListener("resize", refreshHistory);
    resetRuleForm();
    resetProfileForm();
    setActiveAlertTab("rules");
    setSetupModalOpen(false);
    setLayoutEditEnabled(false);
    loadDashboardPreferences().then(async () => {
      await refreshAlertChannelSettings();
      await refreshWatchdogSettings();
      await refreshAll();
    });
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
            trigger_minutes REAL,
            severity TEXT NOT NULL,
            hysteresis_f REAL NOT NULL DEFAULT 5.0,
            notify_cooldown_minutes REAL NOT NULL DEFAULT 15.0,
            active INTEGER NOT NULL DEFAULT 0,
            last_triggered_at TEXT,
            last_triggered_context TEXT
        )
        """
    )
    columns = connection.execute("PRAGMA table_info(alert_rules)").fetchall()
    existing_names = {column["name"] for column in columns}
    if "trigger_minutes" not in existing_names:
        connection.execute(
            "ALTER TABLE alert_rules ADD COLUMN trigger_minutes REAL"
        )
    if "color_hex" not in existing_names:
        connection.execute(
            "ALTER TABLE alert_rules ADD COLUMN color_hex TEXT NOT NULL DEFAULT '#38bdf8'"
        )
    if "notify_email" not in existing_names:
        connection.execute(
            "ALTER TABLE alert_rules ADD COLUMN notify_email INTEGER NOT NULL DEFAULT 0"
        )
    if "notify_sms" not in existing_names:
        connection.execute(
            "ALTER TABLE alert_rules ADD COLUMN notify_sms INTEGER NOT NULL DEFAULT 0"
        )
    if "notify_push" not in existing_names:
        connection.execute(
            "ALTER TABLE alert_rules ADD COLUMN notify_push INTEGER NOT NULL DEFAULT 0"
        )
    if "notify_cooldown_minutes" not in existing_names:
        connection.execute(
            "ALTER TABLE alert_rules ADD COLUMN notify_cooldown_minutes REAL NOT NULL DEFAULT 15.0"
        )
    if "last_triggered_context" not in existing_names:
        connection.execute(
            "ALTER TABLE alert_rules ADD COLUMN last_triggered_context TEXT"
        )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS dashboard_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS firing_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            cone TEXT NOT NULL DEFAULT '',
            segments_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS kiln_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_utc TEXT NOT NULL,
            event_type TEXT NOT NULL,
            label TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_kiln_events_timestamp_utc
        ON kiln_events(timestamp_utc)
        """
    )
    connection.commit()
    return connection


def get_dashboard_state_value(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute(
        "SELECT value FROM dashboard_state WHERE key = ?",
        (key,),
    ).fetchone()
    return None if row is None else row["value"]


def set_dashboard_state_value(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO dashboard_state (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def fetch_dashboard_status() -> dict:
    connection = open_readonly_connection()
    if connection is None:
        return {
            "database_path": str(DATABASE_PATH),
            "total_rows": 0,
            "latest_sample": None,
            "latest_fault": None,
            "latest_alert": None,
            "active_alert_rule": None,
            "active_profile_run": None,
        }

    try:
        fault_acknowledged_at = get_dashboard_state_value(connection, "fault_acknowledged_at")
        alert_acknowledged_at = get_dashboard_state_value(connection, "alert_acknowledged_at")
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
              AND (? IS NULL OR timestamp_utc > ?)
            ORDER BY id DESC
            LIMIT 1
            """
        , (fault_acknowledged_at, fault_acknowledged_at)).fetchone()
        latest_alert = None
        if table_exists(connection, "alert_log"):
            latest_alert_select = "id, timestamp_utc, level, kind, detail, temp_c, temp_f"
            if table_has_column(connection, "alert_log", "rule_name"):
                latest_alert_select += ", rule_name"
            latest_alert = connection.execute(
                f"""
                SELECT {latest_alert_select}
                FROM alert_log
                WHERE (? IS NULL OR timestamp_utc > ?)
                ORDER BY id DESC
                LIMIT 1
                """
            , (alert_acknowledged_at, alert_acknowledged_at)).fetchone()
        active_alert_rule = None
        active_profile_run = fetch_active_profile_run(connection)
        if table_exists(connection, "alert_rules"):
            select_fields = "id, name, enabled, rule_type, threshold_f, severity, hysteresis_f, notify_cooldown_minutes, active, last_triggered_at"
            if table_has_column(connection, "alert_rules", "color_hex"):
                select_fields = "id, name, enabled, rule_type, threshold_f, severity, hysteresis_f, notify_cooldown_minutes, color_hex, active, last_triggered_at"
            active_rows = connection.execute(
                f"""
                SELECT {select_fields}
                FROM alert_rules
                WHERE enabled = 1 AND active = 1
                ORDER BY
                    CASE severity
                        WHEN 'CRITICAL' THEN 3
                        WHEN 'WARNING' THEN 2
                        WHEN 'INFO' THEN 1
                        ELSE 0
                    END DESC,
                    threshold_f DESC,
                    id ASC
                LIMIT 1
                """
            ).fetchone()
            if active_rows is not None:
                active_alert_rule = alert_rule_row_to_payload(active_rows)
        total_rows = connection.execute("SELECT COUNT(*) FROM temperature_log").fetchone()[0]
    finally:
        connection.close()

    return {
        "database_path": str(DATABASE_PATH),
        "total_rows": total_rows,
        "latest_sample": row_to_payload(latest_sample),
        "latest_fault": row_to_payload(latest_fault),
        "latest_alert": row_to_payload(latest_alert),
        "active_alert_rule": active_alert_rule,
        "active_profile_run": active_profile_run,
    }


def fetch_dashboard_preferences() -> dict:
    connection = open_readwrite_connection()
    try:
        theme_json = get_dashboard_state_value(connection, "theme")
        card_order_json = get_dashboard_state_value(connection, "card_order")
        display_unit = get_dashboard_state_value(connection, "display_unit")
    finally:
        connection.close()

    payload: dict = {
        "theme": None,
        "card_order": None,
        "display_unit": "F",
    }

    if theme_json:
        payload["theme"] = json.loads(theme_json)
    if card_order_json:
        payload["card_order"] = json.loads(card_order_json)
    if display_unit in {"F", "C", "BOTH"}:
        payload["display_unit"] = display_unit
    return payload


def update_dashboard_preferences(payload: dict) -> dict:
    connection = open_readwrite_connection()
    try:
        if "theme" in payload:
            theme = payload["theme"]
            if not isinstance(theme, dict):
                raise ValueError("theme must be an object")
            accent = str(theme.get("accent", "#38bdf8")).strip()
            page_bg = str(theme.get("pageBg", "#0b1220")).strip()
            panel_bg = str(theme.get("panelBg", "#111827")).strip()
            for color_value in (accent, page_bg, panel_bg):
                if not color_value.startswith("#") or len(color_value) != 7:
                    raise ValueError("theme colors must be hex values like #112233")
            set_dashboard_state_value(
                connection,
                "theme",
                json.dumps({
                    "accent": accent,
                    "pageBg": page_bg,
                    "panelBg": panel_bg,
                }),
            )

        if "card_order" in payload:
            card_order = payload["card_order"]
            valid_zone_ids = {"top-summary", "below-chart", "sidebar"}
            if not isinstance(card_order, dict):
                raise ValueError("card_order must be a zone mapping")
            if any(zone_id not in valid_zone_ids for zone_id in card_order.keys()):
                raise ValueError("card_order contains an unknown zone")
            if not all(
                isinstance(card_ids, list) and all(isinstance(item, str) for item in card_ids)
                for card_ids in card_order.values()
            ):
                raise ValueError("card_order zone values must be lists of strings")
            set_dashboard_state_value(connection, "card_order", json.dumps(card_order))

        if "display_unit" in payload:
            display_unit = str(payload["display_unit"]).upper()
            if display_unit not in {"F", "C", "BOTH"}:
                raise ValueError("display_unit must be F, C, or BOTH")
            set_dashboard_state_value(connection, "display_unit", display_unit)

        connection.commit()
    finally:
        connection.close()

    return {"ok": True}


def fetch_alert_channel_settings() -> dict:
    settings = load_alert_channel_settings()
    return {"settings": settings}


def update_alert_channel_settings(payload: dict) -> dict:
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")

    base = default_alert_channel_settings()

    normalized_email = settings.get("email", {})
    normalized_sms = settings.get("sms", {})
    normalized_push = settings.get("push", {})
    if not isinstance(normalized_email, dict) or not isinstance(normalized_sms, dict) or not isinstance(normalized_push, dict):
        raise ValueError("email, sms, and push settings must be objects")

    merged = {
        "email": {
            "enabled": bool(normalized_email.get("enabled", base["email"]["enabled"])),
            "smtp_host": str(normalized_email.get("smtp_host", base["email"]["smtp_host"])).strip(),
            "smtp_port": int(normalized_email.get("smtp_port", base["email"]["smtp_port"])),
            "starttls": bool(normalized_email.get("starttls", base["email"]["starttls"])),
            "username": str(normalized_email.get("username", base["email"]["username"])).strip(),
            "password": str(normalized_email.get("password", base["email"]["password"])).strip(),
            "from_addr": str(normalized_email.get("from_addr", base["email"]["from_addr"])).strip(),
            "to_addr": str(normalized_email.get("to_addr", base["email"]["to_addr"])).strip(),
        },
        "sms": {
            "enabled": bool(normalized_sms.get("enabled", base["sms"]["enabled"])),
            "account_sid": str(normalized_sms.get("account_sid", base["sms"]["account_sid"])).strip(),
            "auth_token": str(normalized_sms.get("auth_token", base["sms"]["auth_token"])).strip(),
            "from_number": str(normalized_sms.get("from_number", base["sms"]["from_number"])).strip(),
            "to_number": str(normalized_sms.get("to_number", base["sms"]["to_number"])).strip(),
        },
        "push": {
            "enabled": bool(normalized_push.get("enabled", base["push"]["enabled"])),
            "webhook_url": str(normalized_push.get("webhook_url", base["push"]["webhook_url"])).strip(),
        },
    }

    connection = open_readwrite_connection()
    try:
        set_dashboard_state_value(connection, "alert_channel_settings", json.dumps(merged))
        connection.commit()
    finally:
        connection.close()

    return {"ok": True, "settings": merged}


def default_watchdog_settings() -> dict:
    return {
        "fault_streak_threshold": WATCHDOG_FAULT_STREAK_THRESHOLD,
        "stale_data_seconds": WATCHDOG_STALE_DATA_SECONDS,
        "notify_cooldown_minutes": WATCHDOG_NOTIFY_COOLDOWN_MINUTES,
    }


def fetch_watchdog_settings() -> dict:
    settings = default_watchdog_settings()
    connection = open_readwrite_connection()
    try:
        stored_json = get_dashboard_state_value(connection, "watchdog_settings")
    finally:
        connection.close()

    if stored_json:
        try:
            stored = json.loads(stored_json)
            if isinstance(stored, dict):
                settings.update(stored)
        except json.JSONDecodeError:
            pass

    return {"settings": settings}


def update_watchdog_settings(payload: dict) -> dict:
    settings = payload.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")

    normalized = {
        "fault_streak_threshold": int(settings.get("fault_streak_threshold", WATCHDOG_FAULT_STREAK_THRESHOLD)),
        "stale_data_seconds": float(settings.get("stale_data_seconds", WATCHDOG_STALE_DATA_SECONDS)),
        "notify_cooldown_minutes": float(settings.get("notify_cooldown_minutes", WATCHDOG_NOTIFY_COOLDOWN_MINUTES)),
    }

    if normalized["fault_streak_threshold"] < 1:
        raise ValueError("fault_streak_threshold must be at least 1")
    if normalized["stale_data_seconds"] <= 0:
        raise ValueError("stale_data_seconds must be greater than 0")
    if normalized["notify_cooldown_minutes"] < 0:
        raise ValueError("notify_cooldown_minutes must be zero or greater")

    connection = open_readwrite_connection()
    try:
        set_dashboard_state_value(connection, "watchdog_settings", json.dumps(normalized))
        connection.commit()
    finally:
        connection.close()

    return {"ok": True, "settings": normalized}


def parse_profile_row(row: sqlite3.Row) -> FiringProfile:
    segments_data = json.loads(row["segments_json"])
    if not isinstance(segments_data, list):
        raise ValueError("profile segments must be a list")

    segments = [
        ProfileSegment(
            name=str(segment.get("name", "")).strip(),
            target_temp_c=float(segment.get("target_temp_c")),
            ramp_rate_c_per_hour=float(segment.get("ramp_rate_c_per_hour")),
            soak_minutes=float(segment.get("soak_minutes", 0.0)),
        )
        for segment in segments_data
    ]
    profile = FiringProfile(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        cone=row["cone"],
        segments=segments,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
    validate_profile(profile)
    return profile


def parse_profile_payload(payload: dict) -> FiringProfile:
    segments_payload = payload.get("segments")
    if not isinstance(segments_payload, list):
        raise ValueError("segments must be a list")

    profile = FiringProfile(
        id=None,
        name=str(payload.get("name", "")).strip(),
        description=str(payload.get("description", "")).strip(),
        cone=str(payload.get("cone", "")).strip(),
        segments=[
            ProfileSegment(
                name=str(segment.get("name", "")).strip(),
                target_temp_c=float(segment.get("target_temp_c")),
                ramp_rate_c_per_hour=float(segment.get("ramp_rate_c_per_hour")),
                soak_minutes=float(segment.get("soak_minutes", 0.0)),
            )
            for segment in segments_payload
        ],
    )
    validate_profile(profile)
    return profile


def fetch_profile_by_id(connection: sqlite3.Connection, profile_id: int) -> FiringProfile | None:
    row = connection.execute(
        """
        SELECT id, name, description, cone, segments_json, created_at, updated_at
        FROM firing_profiles
        WHERE id = ?
        """,
        (profile_id,),
    ).fetchone()
    if row is None:
        return None
    return parse_profile_row(row)


def fetch_active_profile_run(connection: sqlite3.Connection) -> dict | None:
    if not table_exists(connection, "dashboard_state") or not table_exists(connection, "firing_profiles"):
        return None

    active_run_json = get_dashboard_state_value(connection, "active_profile_run")
    if not active_run_json:
        return None

    try:
        active_run = json.loads(active_run_json)
    except json.JSONDecodeError:
        return None

    if not isinstance(active_run, dict):
        return None

    profile_id = active_run.get("profile_id")
    started_at_text = active_run.get("started_at")
    start_temp_c = active_run.get("start_temp_c")
    if not isinstance(profile_id, int) or not started_at_text:
        return None

    profile = fetch_profile_by_id(connection, profile_id)
    if profile is None:
        return None

    started_at = datetime.fromisoformat(started_at_text)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    start_temp_c = float(start_temp_c) if start_temp_c is not None else profile.segments[0].target_temp_c
    state = expected_profile_state(profile, started_at, start_temp_c, datetime.now(timezone.utc))
    payload = profile_to_payload(profile)
    payload.update(
        {
            "profile_id": profile.id,
            "started_at": started_at.isoformat(),
            "start_temp_c": start_temp_c,
            "start_temp_f": (start_temp_c * 9.0 / 5.0) + 32.0,
            "phase": state["phase"],
            "segment_index": state["segment_index"],
            "segment_name": state["segment_name"],
            "expected_temp_c": state["expected_temp_c"],
            "expected_temp_f": state["expected_temp_f"],
            "elapsed_seconds": state["elapsed_seconds"],
            "complete": state["complete"],
        }
    )
    return payload


def fetch_profiles() -> dict:
    connection = open_readwrite_connection()
    try:
        rows = connection.execute(
            """
            SELECT id, name, description, cone, segments_json, created_at, updated_at
            FROM firing_profiles
            ORDER BY name COLLATE NOCASE ASC, id ASC
            """
        ).fetchall()
        profiles = [profile_to_payload(parse_profile_row(row)) for row in rows]
        active_run = fetch_active_profile_run(connection)
    finally:
        connection.close()

    return {
        "profiles": profiles,
        "active_run": active_run,
    }


def fetch_camera_status() -> dict:
    try:
        return latest_snapshot_info()
    except CameraError as exc:
        return {
            "available": False,
            "captured_at": None,
            "latest_url": None,
            "archived_filename": None,
            "error": str(exc),
        }


def capture_camera_snapshot() -> dict:
    result = capture_snapshot()
    camera = latest_snapshot_info()
    camera["captured_at"] = result.captured_at
    camera["archived_filename"] = result.archived_filename
    return {
        "ok": True,
        "message": "Snapshot captured.",
        "camera": camera,
    }


def fetch_window_events(connection: sqlite3.Connection, cutoff: str) -> list[dict]:
    if not table_exists(connection, "kiln_events"):
        return []

    rows = connection.execute(
        """
        SELECT id, timestamp_utc, event_type, label, detail
        FROM kiln_events
        WHERE timestamp_utc >= ?
        ORDER BY timestamp_utc DESC, id DESC
        LIMIT 50
        """,
        (cutoff,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "timestamp_utc": row["timestamp_utc"],
            "event_type": row["event_type"],
            "label": row["label"],
            "detail": row["detail"],
        }
        for row in rows
    ]


def build_fault_diagnostics(connection: sqlite3.Connection, cutoff: str) -> dict:
    totals = connection.execute(
        """
        SELECT
            COUNT(*) AS total_samples,
            SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) AS fault_samples,
            SUM(CASE WHEN status = 'OK' THEN 1 ELSE 0 END) AS ok_samples
        FROM temperature_log
        WHERE timestamp_utc >= ?
        """,
        (cutoff,),
    ).fetchone()

    streak_rows = connection.execute(
        """
        SELECT status
        FROM temperature_log
        WHERE timestamp_utc >= ?
        ORDER BY id ASC
        """,
        (cutoff,),
    ).fetchall()
    longest_fault_streak = 0
    running_streak = 0
    for row in streak_rows:
        if row["status"] == "ERROR":
            running_streak += 1
            longest_fault_streak = max(longest_fault_streak, running_streak)
        else:
            running_streak = 0

    current_fault_streak = 0
    current_rows = connection.execute(
        """
        SELECT status
        FROM temperature_log
        ORDER BY id DESC
        LIMIT 250
        """
    ).fetchall()
    for row in current_rows:
        if row["status"] == "ERROR":
            current_fault_streak += 1
        else:
            break

    detail_rows = connection.execute(
        """
        SELECT detail, COUNT(*) AS count
        FROM temperature_log
        WHERE timestamp_utc >= ?
          AND status = 'ERROR'
        GROUP BY detail
        ORDER BY count DESC, detail ASC
        LIMIT 3
        """,
        (cutoff,),
    ).fetchall()

    total_samples = int(totals["total_samples"] or 0)
    fault_samples = int(totals["fault_samples"] or 0)
    return {
        "total_samples": total_samples,
        "fault_samples": fault_samples,
        "ok_samples": int(totals["ok_samples"] or 0),
        "fault_rate_percent": (fault_samples / total_samples * 100.0) if total_samples else 0.0,
        "longest_fault_streak": longest_fault_streak,
        "current_fault_streak": current_fault_streak,
        "top_fault_details": [
            {"detail": row["detail"] or "fault", "count": int(row["count"])}
            for row in detail_rows
        ],
    }


def create_profile(payload: dict) -> dict:
    profile = parse_profile_payload(payload)
    now = datetime.now(timezone.utc).isoformat()
    connection = open_readwrite_connection()
    try:
        connection.execute(
            """
            INSERT INTO firing_profiles (
                name,
                description,
                cone,
                segments_json,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                profile.name,
                profile.description,
                profile.cone,
                json.dumps([segment_to_dict(segment) for segment in profile.segments]),
                now,
                now,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return {"ok": True}


def update_profile(profile_id: int, payload: dict) -> dict:
    profile = parse_profile_payload(payload)
    connection = open_readwrite_connection()
    try:
        existing = fetch_profile_by_id(connection, profile_id)
        if existing is None:
            raise ValueError("profile not found")
        connection.execute(
            """
            UPDATE firing_profiles
            SET name = ?, description = ?, cone = ?, segments_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                profile.name,
                profile.description,
                profile.cone,
                json.dumps([segment_to_dict(segment) for segment in profile.segments]),
                datetime.now(timezone.utc).isoformat(),
                profile_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return {"ok": True}


def delete_profile(profile_id: int) -> dict:
    connection = open_readwrite_connection()
    try:
        connection.execute("DELETE FROM firing_profiles WHERE id = ?", (profile_id,))
        active_run = get_dashboard_state_value(connection, "active_profile_run")
        if active_run:
            try:
                active_payload = json.loads(active_run)
            except json.JSONDecodeError:
                active_payload = None
            if isinstance(active_payload, dict) and active_payload.get("profile_id") == profile_id:
                connection.execute("DELETE FROM dashboard_state WHERE key = 'active_profile_run'")
        connection.commit()
    finally:
        connection.close()
    return {"ok": True}


def activate_profile(profile_id: int) -> dict:
    connection = open_readwrite_connection()
    try:
        profile = fetch_profile_by_id(connection, profile_id)
        if profile is None:
            raise ValueError("profile not found")

        latest_good_sample = None
        if table_exists(connection, "temperature_log"):
            latest_good_sample = connection.execute(
                """
                SELECT temp_c
                FROM temperature_log
                WHERE status = 'OK' AND temp_c IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()

        start_temp_c = (
            float(latest_good_sample["temp_c"])
            if latest_good_sample is not None
            else profile.segments[0].target_temp_c
        )
        run_payload = {
            "profile_id": profile_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "start_temp_c": start_temp_c,
        }
        set_dashboard_state_value(connection, "active_profile_run", json.dumps(run_payload))
        connection.commit()
    finally:
        connection.close()

    return {"ok": True, "message": f"Tracking started for {profile.name}."}


def stop_profile_tracking() -> dict:
    connection = open_readwrite_connection()
    try:
        connection.execute("DELETE FROM dashboard_state WHERE key = 'active_profile_run'")
        connection.commit()
    finally:
        connection.close()
    return {"ok": True, "message": "Profile tracking stopped."}


def segment_to_dict(segment: ProfileSegment) -> dict:
    return {
        "name": segment.name,
        "target_temp_c": segment.target_temp_c,
        "ramp_rate_c_per_hour": segment.ramp_rate_c_per_hour,
        "soak_minutes": segment.soak_minutes,
    }


def fetch_history(window_name: str) -> dict:
    if window_name not in HISTORY_WINDOWS:
        window_name = "24h"

    connection = open_readonly_connection()
    if connection is None:
        return {"range": window_name, "samples": [], "meta": None}

    window_end_dt = datetime.now(timezone.utc)
    window_start_dt = window_end_dt - HISTORY_WINDOWS[window_name]
    cutoff = window_start_dt.isoformat()
    bucket_seconds = HISTORY_BUCKET_PRESETS[window_name]["auto_bucket_seconds"]
    raw_rows = 0
    try:
        raw_rows = connection.execute(
            """
            SELECT COUNT(*)
            FROM temperature_log
            WHERE timestamp_utc >= ?
            """,
            (cutoff,),
        ).fetchone()[0]
        rows = connection.execute(
            """
            SELECT
                MIN(id) AS id,
                MIN(timestamp_utc) AS timestamp_utc,
                CASE
                    WHEN SUM(CASE WHEN status = 'OK' THEN 1 ELSE 0 END) > 0
                    THEN AVG(CASE WHEN status = 'OK' THEN temp_c END)
                    ELSE NULL
                END AS temp_c,
                CASE
                    WHEN SUM(CASE WHEN status = 'OK' THEN 1 ELSE 0 END) > 0
                    THEN AVG(CASE WHEN status = 'OK' THEN temp_f END)
                    ELSE NULL
                END AS temp_f,
                CASE
                    WHEN SUM(CASE WHEN status = 'OK' THEN 1 ELSE 0 END) > 0 THEN 'OK'
                    ELSE 'ERROR'
                END AS status,
                GROUP_CONCAT(DISTINCT CASE WHEN status = 'ERROR' THEN detail END) AS detail
            FROM temperature_log
            WHERE timestamp_utc >= ?
            GROUP BY CAST(strftime('%s', timestamp_utc) AS INTEGER) / ?
            ORDER BY MIN(id) ASC
            """,
            (cutoff, bucket_seconds),
        ).fetchall()
        active_run = fetch_active_profile_run(connection)
        events = fetch_window_events(connection, cutoff)
        diagnostics = build_fault_diagnostics(connection, cutoff)
    finally:
        connection.close()

    profile_overlay = []
    if active_run:
        profile = FiringProfile(
            id=active_run["id"],
            name=active_run["name"],
            description=active_run["description"],
            cone=active_run["cone"],
            segments=[
                ProfileSegment(
                    name=segment["name"],
                    target_temp_c=float(segment["target_temp_c"]),
                    ramp_rate_c_per_hour=float(segment["ramp_rate_c_per_hour"]),
                    soak_minutes=float(segment["soak_minutes"]),
                )
                for segment in active_run["segments"]
            ],
            created_at=active_run.get("created_at"),
            updated_at=active_run.get("updated_at"),
        )
        profile_overlay = generate_profile_overlay(
            profile,
            datetime.fromisoformat(active_run["started_at"]),
            float(active_run["start_temp_c"]),
            window_start_dt,
            window_end_dt,
            bucket_seconds,
        )

    return {
        "range": window_name,
        "samples": [row_to_payload(row) for row in rows],
        "profile_overlay": profile_overlay,
        "events": events,
        "diagnostics": diagnostics,
        "meta": {
            "bucket_seconds": bucket_seconds,
            "returned_samples": len(rows),
            "raw_rows": raw_rows,
        },
    }


def fetch_history_with_resolution(window_name: str, resolution_name: str) -> dict:
    if window_name not in HISTORY_WINDOWS:
        window_name = "24h"

    if resolution_name == "auto":
        return fetch_history(window_name)

    try:
        bucket_seconds = int(resolution_name)
    except ValueError:
        return fetch_history(window_name)

    if bucket_seconds <= 0:
        return fetch_history(window_name)

    connection = open_readonly_connection()
    if connection is None:
        return {"range": window_name, "samples": [], "meta": None}

    window_end_dt = datetime.now(timezone.utc)
    window_start_dt = window_end_dt - HISTORY_WINDOWS[window_name]
    cutoff = window_start_dt.isoformat()
    try:
        raw_rows = connection.execute(
            """
            SELECT COUNT(*)
            FROM temperature_log
            WHERE timestamp_utc >= ?
            """,
            (cutoff,),
        ).fetchone()[0]
        rows = connection.execute(
            """
            SELECT
                MIN(id) AS id,
                MIN(timestamp_utc) AS timestamp_utc,
                CASE
                    WHEN SUM(CASE WHEN status = 'OK' THEN 1 ELSE 0 END) > 0
                    THEN AVG(CASE WHEN status = 'OK' THEN temp_c END)
                    ELSE NULL
                END AS temp_c,
                CASE
                    WHEN SUM(CASE WHEN status = 'OK' THEN 1 ELSE 0 END) > 0
                    THEN AVG(CASE WHEN status = 'OK' THEN temp_f END)
                    ELSE NULL
                END AS temp_f,
                CASE
                    WHEN SUM(CASE WHEN status = 'OK' THEN 1 ELSE 0 END) > 0 THEN 'OK'
                    ELSE 'ERROR'
                END AS status,
                GROUP_CONCAT(DISTINCT CASE WHEN status = 'ERROR' THEN detail END) AS detail
            FROM temperature_log
            WHERE timestamp_utc >= ?
            GROUP BY CAST(strftime('%s', timestamp_utc) AS INTEGER) / ?
            ORDER BY MIN(id) ASC
            """,
            (cutoff, bucket_seconds),
        ).fetchall()
        active_run = fetch_active_profile_run(connection)
        events = fetch_window_events(connection, cutoff)
        diagnostics = build_fault_diagnostics(connection, cutoff)
    finally:
        connection.close()

    profile_overlay = []
    if active_run:
        profile = FiringProfile(
            id=active_run["id"],
            name=active_run["name"],
            description=active_run["description"],
            cone=active_run["cone"],
            segments=[
                ProfileSegment(
                    name=segment["name"],
                    target_temp_c=float(segment["target_temp_c"]),
                    ramp_rate_c_per_hour=float(segment["ramp_rate_c_per_hour"]),
                    soak_minutes=float(segment["soak_minutes"]),
                )
                for segment in active_run["segments"]
            ],
            created_at=active_run.get("created_at"),
            updated_at=active_run.get("updated_at"),
        )
        profile_overlay = generate_profile_overlay(
            profile,
            datetime.fromisoformat(active_run["started_at"]),
            float(active_run["start_temp_c"]),
            window_start_dt,
            window_end_dt,
            bucket_seconds,
        )

    return {
        "range": window_name,
        "samples": [row_to_payload(row) for row in rows],
        "profile_overlay": profile_overlay,
        "events": events,
        "diagnostics": diagnostics,
        "meta": {
            "bucket_seconds": bucket_seconds,
            "returned_samples": len(rows),
            "raw_rows": raw_rows,
        },
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
        select_fields = "id, name, enabled, rule_type, threshold_f, trigger_minutes, severity, hysteresis_f, active, last_triggered_at"
        if table_has_column(connection, "alert_rules", "color_hex"):
            select_fields = (
                "id, name, enabled, rule_type, threshold_f, trigger_minutes, severity, hysteresis_f, "
                "color_hex, notify_email, notify_sms, notify_push, active, last_triggered_at"
            )
        rows = connection.execute(
            f"""
            SELECT {select_fields}
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
                "trigger_minutes": row["trigger_minutes"] if "trigger_minutes" in row.keys() else None,
                "severity": row["severity"],
                "hysteresis_f": row["hysteresis_f"],
                "notify_cooldown_minutes": row["notify_cooldown_minutes"] if "notify_cooldown_minutes" in row.keys() else 15.0,
                "color_hex": row["color_hex"],
                "notify_email": bool(row["notify_email"]),
                "notify_sms": bool(row["notify_sms"]),
                "notify_push": bool(row["notify_push"]),
                "active": bool(row["active"]),
                "last_triggered_at": row["last_triggered_at"],
            }
            for row in rows
        ]
    }


def fetch_alert_deliveries(limit: int = 50) -> dict:
    if not DATABASE_PATH.exists():
        return {"deliveries": []}

    connection = open_readonly_connection()
    if connection is None or not table_exists(connection, "alert_delivery_log"):
        if connection is not None:
            connection.close()
        return {"deliveries": []}

    try:
        rows = connection.execute(
            """
            SELECT
                id,
                timestamp_utc,
                alert_timestamp_utc,
                rule_id,
                rule_name,
                channel,
                success,
                detail
            FROM alert_delivery_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        connection.close()

    return {
        "deliveries": [
            {
                "id": row["id"],
                "timestamp_utc": row["timestamp_utc"],
                "alert_timestamp_utc": row["alert_timestamp_utc"],
                "rule_id": row["rule_id"],
                "rule_name": row["rule_name"],
                "channel": row["channel"],
                "success": bool(row["success"]),
                "detail": row["detail"],
                "sample_age": format_sample_age(row["timestamp_utc"]),
            }
            for row in rows
        ]
    }


def fetch_alert_channel_status() -> dict:
    configured_channels = {
        notifier.channel_name: True
        for notifier in build_enabled_notifiers()
    }
    return {
        "channels": {
            "EMAIL": bool(configured_channels.get("EMAIL")),
            "SMS": bool(configured_channels.get("SMS")),
            "PUSH": bool(configured_channels.get("PUSH")),
        }
    }


def send_test_alert(payload: dict) -> dict:
    requested_channels = payload.get("channels", [])
    if not isinstance(requested_channels, list) or not all(isinstance(item, str) for item in requested_channels):
        raise ValueError("channels must be a list of strings")

    normalized_channels = []
    for channel in requested_channels:
        normalized_channel = channel.strip().upper()
        if normalized_channel not in {"EMAIL", "SMS", "PUSH"}:
            raise ValueError(f"unsupported test alert channel: {channel}")
        if normalized_channel not in normalized_channels:
            normalized_channels.append(normalized_channel)

    if not normalized_channels:
        raise ValueError("at least one test alert channel is required")

    now = datetime.now(timezone.utc)
    alert = AlertEvent(
        timestamp_utc=now.isoformat(),
        level="INFO",
        kind="TEST_ALERT",
        detail="Dashboard test alert requested from the kiln monitor dashboard.",
        temp_c=None,
        temp_f=None,
        rule_id=0,
        rule_name="Dashboard Test Alert",
    )
    rule = AlertRule(
        id=0,
        name="Dashboard Test Alert",
        enabled=True,
        rule_type="TARGET_REACHED",
        threshold_f=0.0,
        trigger_minutes=None,
        severity="INFO",
        hysteresis_f=0.0,
        notify_cooldown_minutes=0.0,
        color_hex="#38bdf8",
        notify_email="EMAIL" in normalized_channels,
        notify_sms="SMS" in normalized_channels,
        notify_push="PUSH" in normalized_channels,
        active=False,
        last_triggered_at=None,
        last_triggered_context=None,
    )
    notifiers = {
        notifier.channel_name: notifier
        for notifier in build_enabled_notifiers()
    }

    storage = SQLiteLogger(DATABASE_PATH)
    results: list[dict] = []
    try:
        for channel in normalized_channels:
            notifier = notifiers.get(channel)
            if notifier is None:
                detail = "channel is not configured globally"
                storage.log_alert_delivery(alert, channel=channel, success=False, detail=detail)
                results.append({"channel": channel, "success": False, "detail": detail})
                continue

            try:
                result = notifier.send(alert, rule)
                storage.log_alert_delivery(
                    alert,
                    channel=result.channel,
                    success=result.success,
                    detail=result.detail,
                )
                results.append(
                    {"channel": result.channel, "success": result.success, "detail": result.detail}
                )
            except NotificationError as exc:
                detail = str(exc)
                storage.log_alert_delivery(alert, channel=channel, success=False, detail=detail)
                results.append({"channel": channel, "success": False, "detail": detail})
    finally:
        storage.close()

    success_count = sum(1 for result in results if result["success"])
    return {
        "ok": True,
        "message": f"Test alert sent on {success_count} of {len(results)} requested channel(s).",
        "results": results,
    }


def parse_alert_rule_payload(payload: dict) -> AlertRule:
    rule_type = str(payload.get("rule_type", "")).strip().upper()
    trigger_minutes = payload.get("trigger_minutes")
    rule = AlertRule(
        id=None,
        name=str(payload.get("name", "")).strip(),
        enabled=bool(payload.get("enabled", True)),
        rule_type=rule_type,
        threshold_f=float(payload.get("threshold_f")),
        trigger_minutes=None if trigger_minutes in {None, ""} else float(trigger_minutes),
        severity=str(payload.get("severity", "")).strip().upper(),
        hysteresis_f=float(payload.get("hysteresis_f", 0.0)),
        notify_cooldown_minutes=float(payload.get("notify_cooldown_minutes", 15.0)),
        color_hex=str(payload.get("color_hex", "#38bdf8")).strip(),
        notify_email=bool(payload.get("notify_email", False)),
        notify_sms=bool(payload.get("notify_sms", False)),
        notify_push=bool(payload.get("notify_push", False)),
        active=False,
        last_triggered_at=None,
        last_triggered_context=None,
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
                trigger_minutes,
                severity,
                hysteresis_f,
                notify_cooldown_minutes,
                color_hex,
                notify_email,
                notify_sms,
                notify_push,
                active,
                last_triggered_at,
                last_triggered_context
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
            """,
            (
                rule.name,
                int(rule.enabled),
                rule.rule_type,
                rule.threshold_f,
                rule.trigger_minutes,
                rule.severity,
                rule.hysteresis_f,
                rule.notify_cooldown_minutes,
                rule.color_hex,
                int(rule.notify_email),
                int(rule.notify_sms),
                int(rule.notify_push),
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
            SET name = ?, enabled = ?, rule_type = ?, threshold_f = ?, trigger_minutes = ?, severity = ?, hysteresis_f = ?, notify_cooldown_minutes = ?, color_hex = ?,
                notify_email = ?, notify_sms = ?, notify_push = ?,
                active = CASE WHEN ? = 1 THEN active ELSE 0 END
            WHERE id = ?
            """,
            (
                rule.name,
                int(rule.enabled),
                rule.rule_type,
                rule.threshold_f,
                rule.trigger_minutes,
                rule.severity,
                rule.hysteresis_f,
                rule.notify_cooldown_minutes,
                rule.color_hex,
                int(rule.notify_email),
                int(rule.notify_sms),
                int(rule.notify_push),
                int(rule.enabled),
                rule_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return {"ok": True}


def clone_alert_rule(rule_id: int) -> dict:
    connection = open_readwrite_connection()
    try:
        row = connection.execute(
            """
            SELECT
                name,
                enabled,
                rule_type,
                threshold_f,
                trigger_minutes,
                severity,
                hysteresis_f,
                notify_cooldown_minutes,
                color_hex,
                notify_email,
                notify_sms,
                notify_push
            FROM alert_rules
            WHERE id = ?
            """,
            (rule_id,),
        ).fetchone()
        if row is None:
            raise ValueError("alert rule not found")

        connection.execute(
            """
            INSERT INTO alert_rules (
                name,
                enabled,
                rule_type,
                threshold_f,
                trigger_minutes,
                severity,
                hysteresis_f,
                notify_cooldown_minutes,
                color_hex,
                notify_email,
                notify_sms,
                notify_push,
                active,
                last_triggered_at,
                last_triggered_context
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
            """,
            (
                f"{row['name']} Copy",
                int(bool(row["enabled"])),
                row["rule_type"],
                row["threshold_f"],
                row["trigger_minutes"],
                row["severity"],
                row["hysteresis_f"],
                row["notify_cooldown_minutes"],
                row["color_hex"],
                int(bool(row["notify_email"])),
                int(bool(row["notify_sms"])),
                int(bool(row["notify_push"])),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return {"ok": True}


def create_event_marker(payload: dict) -> dict:
    label = str(payload.get("label", "")).strip()
    detail = str(payload.get("detail", "")).strip()
    event_type = str(payload.get("event_type", "NOTE")).strip().upper() or "NOTE"
    if not label:
        raise ValueError("event label is required")

    connection = open_readwrite_connection()
    try:
        timestamp_utc = datetime.now(timezone.utc).isoformat()
        connection.execute(
            """
            INSERT INTO kiln_events (
                timestamp_utc,
                event_type,
                label,
                detail
            ) VALUES (?, ?, ?, ?)
            """,
            (timestamp_utc, event_type, label, detail),
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


def reset_faults() -> dict:
    connection = open_readwrite_connection()
    try:
        set_dashboard_state_value(connection, "fault_acknowledged_at", datetime.now(timezone.utc).isoformat())
        connection.commit()
    finally:
        connection.close()
    return {"ok": True}


def reset_alerts() -> dict:
    connection = open_readwrite_connection()
    try:
        connection.execute("UPDATE alert_rules SET active = 0")
        set_dashboard_state_value(connection, "alert_acknowledged_at", datetime.now(timezone.utc).isoformat())
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


def alert_rule_row_to_payload(row: sqlite3.Row) -> dict:
    payload = {
        "id": row["id"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "rule_type": row["rule_type"],
        "threshold_f": row["threshold_f"],
        "trigger_minutes": row["trigger_minutes"] if "trigger_minutes" in row.keys() else None,
        "severity": row["severity"],
        "hysteresis_f": row["hysteresis_f"],
        "notify_cooldown_minutes": row["notify_cooldown_minutes"] if "notify_cooldown_minutes" in row.keys() else 15.0,
        "active": bool(row["active"]),
        "last_triggered_at": row["last_triggered_at"],
        "color_hex": row["color_hex"] if "color_hex" in row.keys() else "#38bdf8",
        "notify_email": bool(row["notify_email"]) if "notify_email" in row.keys() else False,
        "notify_sms": bool(row["notify_sms"]) if "notify_sms" in row.keys() else False,
        "notify_push": bool(row["notify_push"]) if "notify_push" in row.keys() else False,
    }
    return payload


class DashboardRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed_path = urlparse(self.path)

        if parsed_path.path == "/":
            self.send_text_response(PAGE_HTML, content_type="text/html; charset=utf-8")
            return

        if parsed_path.path == "/camera/latest.jpg":
            latest_path = CAMERA_SNAPSHOTS_DIR / "latest.jpg"
            if not latest_path.exists():
                self.send_error(404, "No snapshot available")
                return
            try:
                image_bytes = latest_path.read_bytes()
            except OSError:
                self.send_error(500, "Unable to read snapshot")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(image_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(image_bytes)
            return

        if parsed_path.path == "/api/status":
            self.send_json_response(fetch_dashboard_status())
            return

        if parsed_path.path == "/api/history":
            query = parse_qs(parsed_path.query)
            range_name = query.get("range", ["24h"])[0]
            resolution_name = query.get("resolution", ["auto"])[0]
            self.send_json_response(fetch_history_with_resolution(range_name, resolution_name))
            return

        if parsed_path.path == "/api/alert-rules":
            self.send_json_response(fetch_alert_rules())
            return

        if parsed_path.path == "/api/alert-deliveries":
            self.send_json_response(fetch_alert_deliveries())
            return

        if parsed_path.path == "/api/alert-channels":
            self.send_json_response(fetch_alert_channel_status())
            return

        if parsed_path.path == "/api/alert-channel-settings":
            self.send_json_response(fetch_alert_channel_settings())
            return

        if parsed_path.path == "/api/watchdog-settings":
            self.send_json_response(fetch_watchdog_settings())
            return

        if parsed_path.path == "/api/profiles":
            self.send_json_response(fetch_profiles())
            return

        if parsed_path.path == "/api/camera/status":
            self.send_json_response(fetch_camera_status())
            return

        if parsed_path.path == "/api/dashboard-preferences":
            self.send_json_response(fetch_dashboard_preferences())
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

            if parsed_path.path == "/api/dashboard-preferences":
                self.send_json_response(update_dashboard_preferences(payload))
                return

            if parsed_path.path == "/api/alert-channel-settings":
                self.send_json_response(update_alert_channel_settings(payload))
                return

            if parsed_path.path == "/api/watchdog-settings":
                self.send_json_response(update_watchdog_settings(payload))
                return

            if parsed_path.path == "/api/profiles":
                self.send_json_response(create_profile(payload))
                return

            if parsed_path.path == "/api/events":
                self.send_json_response(create_event_marker(payload))
                return

            if parsed_path.path == "/api/camera/capture":
                self.send_json_response(capture_camera_snapshot())
                return

            if parsed_path.path == "/api/profiles/stop":
                self.send_json_response(stop_profile_tracking())
                return

            if parsed_path.path == "/api/reset-faults":
                self.send_json_response(reset_faults())
                return

            if parsed_path.path == "/api/reset-alerts":
                self.send_json_response(reset_alerts())
                return

            if parsed_path.path == "/api/test-alert":
                self.send_json_response(send_test_alert(payload))
                return

            if parsed_path.path.startswith("/api/alert-rules/") and parsed_path.path.endswith("/delete"):
                rule_id = int(parsed_path.path.split("/")[3])
                self.send_json_response(delete_alert_rule(rule_id))
                return

            if parsed_path.path.startswith("/api/alert-rules/") and parsed_path.path.endswith("/clone"):
                rule_id = int(parsed_path.path.split("/")[3])
                self.send_json_response(clone_alert_rule(rule_id))
                return

            if parsed_path.path.startswith("/api/profiles/") and parsed_path.path.endswith("/delete"):
                profile_id = int(parsed_path.path.split("/")[3])
                self.send_json_response(delete_profile(profile_id))
                return

            if parsed_path.path.startswith("/api/profiles/") and parsed_path.path.endswith("/activate"):
                profile_id = int(parsed_path.path.split("/")[3])
                self.send_json_response(activate_profile(profile_id))
                return

            if parsed_path.path.startswith("/api/alert-rules/"):
                rule_id = int(parsed_path.path.split("/")[3])
                self.send_json_response(update_alert_rule(rule_id, payload))
                return

            if parsed_path.path.startswith("/api/profiles/"):
                profile_id = int(parsed_path.path.split("/")[3])
                self.send_json_response(update_profile(profile_id, payload))
                return
        except ValueError as exc:
            self.send_json_response({"error": str(exc)}, status=400)
            return
        except CameraError as exc:
            self.send_json_response({"error": str(exc)}, status=500)
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
