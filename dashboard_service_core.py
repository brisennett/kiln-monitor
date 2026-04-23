from __future__ import annotations

import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alerts import AlertEvent, AlertRule, validate_rule
from camera import CameraError, capture_snapshot, latest_snapshot_info, list_recent_snapshots
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
FIRING_LOG_PHOTOS_DIR = DATABASE_PATH.parent / "firing_log_photos"

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

def normalize_iso_utc(timestamp_text: str | None) -> str | None:
    if timestamp_text is None:
        return None
    cleaned = str(timestamp_text).strip()
    if not cleaned:
        return None
    parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()

def parse_snapshot_captured_at(snapshot: dict) -> datetime | None:
    captured_at = snapshot.get("captured_at")
    if not captured_at:
        return None
    try:
        parsed = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def sanitize_uploaded_photo_name(filename: str) -> str:
    base_name = Path(filename or "photo").name
    sanitized = "".join(character if character.isalnum() or character in {"-", "_", "."} else "_" for character in base_name)
    sanitized = sanitized.strip("._") or "photo"
    return sanitized

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
            detail TEXT NOT NULL DEFAULT '',
            temp_c REAL,
            temp_f REAL,
            sample_status TEXT,
            sample_detail TEXT
        )
        """
    )
    event_columns = connection.execute("PRAGMA table_info(kiln_events)").fetchall()
    existing_event_names = {column["name"] for column in event_columns}
    if "temp_c" not in existing_event_names:
        connection.execute("ALTER TABLE kiln_events ADD COLUMN temp_c REAL")
    if "temp_f" not in existing_event_names:
        connection.execute("ALTER TABLE kiln_events ADD COLUMN temp_f REAL")
    if "sample_status" not in existing_event_names:
        connection.execute("ALTER TABLE kiln_events ADD COLUMN sample_status TEXT")
    if "sample_detail" not in existing_event_names:
        connection.execute("ALTER TABLE kiln_events ADD COLUMN sample_detail TEXT")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_kiln_events_timestamp_utc
        ON kiln_events(timestamp_utc)
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS firing_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            firing_type TEXT NOT NULL DEFAULT 'OTHER',
            planned_cone TEXT NOT NULL DEFAULT '',
            started_at_utc TEXT NOT NULL,
            ended_at_utc TEXT,
            description TEXT NOT NULL DEFAULT '',
            result_summary TEXT NOT NULL DEFAULT '',
            result_status TEXT NOT NULL DEFAULT 'PENDING',
            post_mortem TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS firing_log_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            firing_log_id INTEGER NOT NULL,
            event_id INTEGER,
            timestamp_utc TEXT NOT NULL,
            event_type TEXT NOT NULL,
            label TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            temp_c REAL,
            temp_f REAL,
            sample_status TEXT,
            sample_detail TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS firing_log_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            firing_log_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            captured_at_utc TEXT,
            source_type TEXT NOT NULL DEFAULT 'AUTO',
            original_filename TEXT,
            caption TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )
    if not table_has_column(connection, "firing_log_snapshots", "source_type"):
        connection.execute("ALTER TABLE firing_log_snapshots ADD COLUMN source_type TEXT NOT NULL DEFAULT 'AUTO'")
    if not table_has_column(connection, "firing_log_snapshots", "original_filename"):
        connection.execute("ALTER TABLE firing_log_snapshots ADD COLUMN original_filename TEXT")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_firing_logs_started_at_utc
        ON firing_logs(started_at_utc)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_firing_log_events_log_id
        ON firing_log_events(firing_log_id, timestamp_utc)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_firing_log_snapshots_log_id
        ON firing_log_snapshots(firing_log_id, captured_at_utc)
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

def segment_to_dict(segment: ProfileSegment) -> dict:
    return {
        "name": segment.name,
        "target_temp_c": segment.target_temp_c,
        "ramp_rate_c_per_hour": segment.ramp_rate_c_per_hour,
        "soak_minutes": segment.soak_minutes,
    }

def fetch_window_events(connection: sqlite3.Connection, cutoff: str) -> list[dict]:
    if not table_exists(connection, "kiln_events"):
        return []

    select_fields = "id, timestamp_utc, event_type, label, detail"
    if table_has_column(connection, "kiln_events", "temp_c"):
        select_fields += ", temp_c"
    if table_has_column(connection, "kiln_events", "temp_f"):
        select_fields += ", temp_f"
    if table_has_column(connection, "kiln_events", "sample_status"):
        select_fields += ", sample_status"
    if table_has_column(connection, "kiln_events", "sample_detail"):
        select_fields += ", sample_detail"
    rows = connection.execute(
        f"""
        SELECT {select_fields}
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
            "temp_c": row["temp_c"] if "temp_c" in row.keys() else None,
            "temp_f": row["temp_f"] if "temp_f" in row.keys() else None,
            "sample_status": row["sample_status"] if "sample_status" in row.keys() else None,
            "sample_detail": row["sample_detail"] if "sample_detail" in row.keys() else None,
        }
        for row in rows
    ]

def build_fault_diagnostics(connection: sqlite3.Connection, cutoff: str, end_utc: str | None = None) -> dict:
    end_clause = " AND timestamp_utc <= ?" if end_utc else ""
    totals_params: tuple[object, ...] = (cutoff, end_utc) if end_utc else (cutoff,)
    totals = connection.execute(
        f"""
        SELECT
            COUNT(*) AS total_samples,
            SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) AS fault_samples,
            SUM(CASE WHEN status = 'OK' THEN 1 ELSE 0 END) AS ok_samples
        FROM temperature_log
        WHERE timestamp_utc >= ?
        {end_clause}
        """,
        totals_params,
    ).fetchone()

    streak_params: tuple[object, ...] = (cutoff, end_utc) if end_utc else (cutoff,)
    streak_rows = connection.execute(
        f"""
        SELECT status
        FROM temperature_log
        WHERE timestamp_utc >= ?
        {end_clause}
        ORDER BY id ASC
        """,
        streak_params,
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
    current_where = "WHERE 1=1"
    current_params: tuple[object, ...] = ()
    if end_utc:
        current_where = "WHERE timestamp_utc <= ?"
        current_params = (end_utc,)
    current_rows = connection.execute(
        f"""
        SELECT status
        FROM temperature_log
        {current_where}
        ORDER BY id DESC
        LIMIT 250
        """,
        current_params,
    ).fetchall()
    for row in current_rows:
        if row["status"] == "ERROR":
            current_fault_streak += 1
        else:
            break

    detail_params: tuple[object, ...] = (cutoff, end_utc) if end_utc else (cutoff,)
    detail_rows = connection.execute(
        f"""
        SELECT detail, COUNT(*) AS count
        FROM temperature_log
        WHERE timestamp_utc >= ?
          {f"AND timestamp_utc <= ?" if end_utc else ""}
          AND status = 'ERROR'
        GROUP BY detail
        ORDER BY count DESC, detail ASC
        LIMIT 3
        """,
        detail_params,
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

