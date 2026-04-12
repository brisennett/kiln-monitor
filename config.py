from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ARCHIVE_DIR = Path(os.getenv("KILN_MONITOR_ARCHIVE_DIR", DATA_DIR / "archive"))
LOG_DIR = BASE_DIR / "logs"
DATABASE_PATH = Path(os.getenv("KILN_MONITOR_DB_PATH", DATA_DIR / "kiln_monitor.db"))
APP_LOG_PATH = Path(os.getenv("KILN_MONITOR_LOG_PATH", LOG_DIR / "kiln_monitor.log"))

# Sensor polling cadence. A 2-second default is responsive enough for a kiln
# while keeping storage growth and sensor bus traffic modest.
READ_INTERVAL_SECONDS = float(os.getenv("KILN_MONITOR_READ_INTERVAL_SECONDS", "2.0"))

# Reject single-sample jumps larger than this threshold from the previous
# accepted reading. This helps discard reconnect glitches and bad contacts.
MAX_SAMPLE_JUMP_C = float(os.getenv("KILN_MONITOR_MAX_SAMPLE_JUMP_C", "50.0"))

# Consecutive read failures before the monitor increases log severity.
ERROR_STREAK_WARNING_THRESHOLD = int(os.getenv("KILN_MONITOR_ERROR_STREAK_WARNING_THRESHOLD", "3"))

# Watchdog thresholds for appliance-style health alerts. These generate
# built-in alerts when the monitor is alive but sensor health or data freshness
# has gone bad for longer than expected.
WATCHDOG_FAULT_STREAK_THRESHOLD = int(os.getenv("KILN_MONITOR_WATCHDOG_FAULT_STREAK_THRESHOLD", "5"))
WATCHDOG_STALE_DATA_SECONDS = float(os.getenv("KILN_MONITOR_WATCHDOG_STALE_DATA_SECONDS", "30"))
WATCHDOG_NOTIFY_COOLDOWN_MINUTES = float(os.getenv("KILN_MONITOR_WATCHDOG_NOTIFY_COOLDOWN_MINUTES", "30"))

# Sensor front-end model. Use MAX31855 for the replacement board or MAX31856
# for the original thermocouple amplifier board.
SENSOR_MODEL = os.getenv("KILN_MONITOR_SENSOR_MODEL", "MAX31855").upper()

# Thermocouple configuration.
THERMOCOUPLE_TYPE = os.getenv("KILN_MONITOR_THERMOCOUPLE_TYPE", "K").upper()

# Named board pin used for MAX31856 chip select.
SPI_CS_PIN = os.getenv(
    "KILN_MONITOR_SPI_CS_PIN",
    os.getenv("KILN_MONITOR_MAX31856_CS_PIN", "D5"),
).upper()

# Backward-compatible alias for older service/env settings.
MAX31856_CS_PIN = SPI_CS_PIN

# SQLite durability mode. FULL is safer for an appliance-style logger where write
# rate is low and preserving recent samples across power loss matters.
SQLITE_SYNCHRONOUS_MODE = os.getenv("KILN_MONITOR_SQLITE_SYNCHRONOUS_MODE", "FULL").upper()

# Print status to the console every N successful samples.
STATUS_EVERY_N_SAMPLES = int(os.getenv("KILN_MONITOR_STATUS_EVERY_N_SAMPLES", "1"))

# Retention policy for raw SQLite rows and compressed CSV archives.
RETENTION_SQLITE_DAYS = int(os.getenv("KILN_MONITOR_RETENTION_SQLITE_DAYS", "30"))
RETENTION_ARCHIVE_DAYS = int(os.getenv("KILN_MONITOR_RETENTION_ARCHIVE_DAYS", "183"))

# Alert delivery channels. Each channel can be enabled globally here, then
# enabled per alert rule in the dashboard.
ALERT_EMAIL_ENABLED = os.getenv("KILN_MONITOR_ALERT_EMAIL_ENABLED", "false").lower() == "true"
ALERT_EMAIL_SMTP_HOST = os.getenv("KILN_MONITOR_ALERT_EMAIL_SMTP_HOST", "")
ALERT_EMAIL_SMTP_PORT = int(os.getenv("KILN_MONITOR_ALERT_EMAIL_SMTP_PORT", "587"))
ALERT_EMAIL_SMTP_STARTTLS = os.getenv("KILN_MONITOR_ALERT_EMAIL_SMTP_STARTTLS", "true").lower() == "true"
ALERT_EMAIL_SMTP_USERNAME = os.getenv("KILN_MONITOR_ALERT_EMAIL_SMTP_USERNAME", "")
ALERT_EMAIL_SMTP_PASSWORD = os.getenv("KILN_MONITOR_ALERT_EMAIL_SMTP_PASSWORD", "")
ALERT_EMAIL_FROM = os.getenv("KILN_MONITOR_ALERT_EMAIL_FROM", "")
ALERT_EMAIL_TO = os.getenv("KILN_MONITOR_ALERT_EMAIL_TO", "")

ALERT_SMS_ENABLED = os.getenv("KILN_MONITOR_ALERT_SMS_ENABLED", "false").lower() == "true"
ALERT_TWILIO_ACCOUNT_SID = os.getenv("KILN_MONITOR_TWILIO_ACCOUNT_SID", "")
ALERT_TWILIO_AUTH_TOKEN = os.getenv("KILN_MONITOR_TWILIO_AUTH_TOKEN", "")
ALERT_TWILIO_FROM = os.getenv("KILN_MONITOR_TWILIO_FROM", "")
ALERT_SMS_TO = os.getenv("KILN_MONITOR_ALERT_SMS_TO", "")

ALERT_PUSH_ENABLED = os.getenv("KILN_MONITOR_ALERT_PUSH_ENABLED", "false").lower() == "true"
ALERT_PUSH_WEBHOOK_URL = os.getenv("KILN_MONITOR_ALERT_PUSH_WEBHOOK_URL", "")


def load_watchdog_settings() -> dict:
    settings = {
        "fault_streak_threshold": WATCHDOG_FAULT_STREAK_THRESHOLD,
        "stale_data_seconds": WATCHDOG_STALE_DATA_SECONDS,
        "notify_cooldown_minutes": WATCHDOG_NOTIFY_COOLDOWN_MINUTES,
    }
    if not DATABASE_PATH.exists():
        return settings

    connection = sqlite3.connect(DATABASE_PATH)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT value
            FROM dashboard_state
            WHERE key = ?
            """,
            ("watchdog_settings",),
        ).fetchone()
        if row is None or not row["value"]:
            return settings

        payload = json.loads(row["value"])
        if not isinstance(payload, dict):
            return settings

        if "fault_streak_threshold" in payload:
            settings["fault_streak_threshold"] = int(payload["fault_streak_threshold"])
        if "stale_data_seconds" in payload:
            settings["stale_data_seconds"] = float(payload["stale_data_seconds"])
        if "notify_cooldown_minutes" in payload:
            settings["notify_cooldown_minutes"] = float(payload["notify_cooldown_minutes"])
        return settings
    except (sqlite3.Error, json.JSONDecodeError, ValueError, TypeError):
        return settings
    finally:
        connection.close()
