from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

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


HOST = "0.0.0.0"
PORT = 8080
UI_DIR = Path(__file__).resolve().parent / "ui"
STATIC_DIR = Path(__file__).resolve().parent / "static"
FIRING_LOG_PHOTOS_DIR = DATABASE_PATH.parent / "firing_log_photos"
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


def load_ui_page(filename: str) -> str:
    return (UI_DIR / filename).read_text(encoding="utf-8")


def resolve_static_path(request_path: str) -> Path | None:
    if not request_path.startswith("/static/"):
        return None

    relative_path = Path(unquote(request_path.removeprefix("/static/")))
    candidate = (STATIC_DIR / relative_path).resolve()
    try:
        candidate.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate

DASHBOARD_PAGE_HTML = load_ui_page("dashboard.html")

ALERTS_PAGE_HTML = load_ui_page("alerts.html")

EVENTS_PAGE_HTML = load_ui_page("events.html")

FAULTS_PAGE_HTML = load_ui_page("faults.html")

FIRING_LOGS_PAGE_HTML = load_ui_page("logs.html")

PANEL_PAGE_HTML = load_ui_page("panel.html")


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
    normalized_slack = settings.get("slack", settings.get("push", {}))
    if not isinstance(normalized_email, dict) or not isinstance(normalized_sms, dict) or not isinstance(normalized_slack, dict):
        raise ValueError("email, sms, and slack settings must be objects")

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
        "slack": {
            "enabled": bool(normalized_slack.get("enabled", base["slack"]["enabled"])),
            "webhook_url": str(normalized_slack.get("webhook_url", base["slack"]["webhook_url"])).strip(),
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
        camera = latest_snapshot_info()
        snapshots = list_recent_snapshots(limit=20)
        latest_snapshot = snapshots[0] if snapshots else None
        if latest_snapshot is None and camera.get("available") and camera.get("latest_url"):
            latest_snapshot = {
                "filename": camera.get("latest_display_name") or "Latest snapshot",
                "captured_at": camera.get("captured_at"),
                "url": camera.get("latest_url"),
            }
        camera["snapshots"] = snapshots
        camera["latest_snapshot"] = latest_snapshot
        return camera
    except CameraError as exc:
        return {
            "available": False,
            "captured_at": None,
            "latest_url": None,
            "archived_filename": None,
            "latest_display_name": None,
            "snapshots": [],
            "latest_snapshot": None,
            "error": str(exc),
        }


def capture_camera_snapshot() -> dict:
    result = capture_snapshot()
    camera = fetch_camera_status()
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


def _auto_bucket_for_span(span_seconds: float) -> int:
    if span_seconds <= 3600:
        return 2
    if span_seconds <= 6 * 3600:
        return 30
    if span_seconds <= 24 * 3600:
        return 600
    if span_seconds <= 3 * 24 * 3600:
        return 1800
    return 3600


def fetch_history_between(start_dt: datetime, end_dt: datetime, resolution_name: str = "auto") -> dict:
    if end_dt <= start_dt:
        raise ValueError("end must be after start")

    connection = open_readonly_connection()
    if connection is None:
        return {"range": "custom", "samples": [], "meta": None}

    span_seconds = max(1.0, (end_dt - start_dt).total_seconds())
    if resolution_name == "auto":
        bucket_seconds = _auto_bucket_for_span(span_seconds)
    else:
        try:
            bucket_seconds = int(resolution_name)
        except ValueError:
            bucket_seconds = _auto_bucket_for_span(span_seconds)
        if bucket_seconds <= 0:
            bucket_seconds = _auto_bucket_for_span(span_seconds)

    start_iso = start_dt.isoformat()
    end_iso = end_dt.isoformat()
    try:
        raw_rows = connection.execute(
            """
            SELECT COUNT(*)
            FROM temperature_log
            WHERE timestamp_utc >= ? AND timestamp_utc <= ?
            """,
            (start_iso, end_iso),
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
            WHERE timestamp_utc >= ? AND timestamp_utc <= ?
            GROUP BY CAST(strftime('%s', timestamp_utc) AS INTEGER) / ?
            ORDER BY MIN(id) ASC
            """,
            (start_iso, end_iso, bucket_seconds),
        ).fetchall()
        active_run = fetch_active_profile_run(connection)
        events = fetch_window_events(connection, start_iso)
        events = [event for event in events if event["timestamp_utc"] <= end_iso]
        diagnostics = build_fault_diagnostics(connection, start_iso, end_iso)
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
            start_dt,
            end_dt,
            bucket_seconds,
        )

    return {
        "range": "custom",
        "samples": [row_to_payload(row) for row in rows],
        "profile_overlay": profile_overlay,
        "events": events,
        "diagnostics": diagnostics,
        "meta": {
            "bucket_seconds": bucket_seconds,
            "returned_samples": len(rows),
            "raw_rows": raw_rows,
            "start_utc": start_iso,
            "end_utc": end_iso,
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


def fetch_recent_alerts(limit: int = 6) -> dict:
    if not DATABASE_PATH.exists():
        return {"alerts": []}

    connection = open_readonly_connection()
    if connection is None or not table_exists(connection, "alert_log"):
        if connection is not None:
            connection.close()
        return {"alerts": []}

    try:
        select_fields = "id, timestamp_utc, level, kind, detail, temp_f"
        if table_has_column(connection, "alert_log", "rule_name"):
            select_fields += ", rule_name"
        rows = connection.execute(
            f"""
            SELECT {select_fields}
            FROM alert_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        connection.close()

    return {
        "alerts": [
            {
                "id": row["id"],
                "timestamp_utc": row["timestamp_utc"],
                "level": row["level"],
                "kind": row["kind"],
                "detail": row["detail"],
                "temp_f": row["temp_f"],
                "rule_name": row["rule_name"] if "rule_name" in row.keys() else None,
                "sample_age": format_sample_age(row["timestamp_utc"]),
            }
            for row in rows
        ]
    }


def parse_firing_log_payload(payload: dict) -> dict:
    title = str(payload.get("title", "")).strip()
    if not title:
        raise ValueError("firing log title is required")

    started_at_utc = normalize_iso_utc(payload.get("started_at_utc"))
    if not started_at_utc:
        raise ValueError("start time is required")

    ended_at_utc = normalize_iso_utc(payload.get("ended_at_utc"))
    if ended_at_utc and ended_at_utc < started_at_utc:
        raise ValueError("end time must be after start time")

    firing_type = str(payload.get("firing_type", "OTHER")).strip().upper() or "OTHER"
    result_status = str(payload.get("result_status", "PENDING")).strip().upper() or "PENDING"

    return {
        "title": title,
        "firing_type": firing_type,
        "planned_cone": str(payload.get("planned_cone", "")).strip(),
        "started_at_utc": started_at_utc,
        "ended_at_utc": ended_at_utc,
        "description": str(payload.get("description", "")).strip(),
        "result_summary": str(payload.get("result_summary", "")).strip(),
        "result_status": result_status,
        "post_mortem": str(payload.get("post_mortem", "")).strip(),
    }


def sync_firing_log_related_data(
    connection: sqlite3.Connection,
    firing_log_id: int,
    *,
    started_at_utc: str,
    ended_at_utc: str | None,
) -> None:
    sync_cutoff = ended_at_utc or datetime.now(timezone.utc).isoformat()
    now = datetime.now(timezone.utc).isoformat()

    connection.execute("DELETE FROM firing_log_events WHERE firing_log_id = ?", (firing_log_id,))
    connection.execute(
        "DELETE FROM firing_log_snapshots WHERE firing_log_id = ? AND source_type = 'AUTO'",
        (firing_log_id,),
    )

    if table_exists(connection, "kiln_events"):
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
            WHERE timestamp_utc >= ? AND timestamp_utc <= ?
            ORDER BY timestamp_utc ASC, id ASC
            """,
            (started_at_utc, sync_cutoff),
        ).fetchall()
        for row in rows:
            connection.execute(
                """
                INSERT INTO firing_log_events (
                    firing_log_id,
                    event_id,
                    timestamp_utc,
                    event_type,
                    label,
                    detail,
                    temp_c,
                    temp_f,
                    sample_status,
                    sample_detail,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    firing_log_id,
                    row["id"],
                    row["timestamp_utc"],
                    row["event_type"],
                    row["label"],
                    row["detail"],
                    row["temp_c"] if "temp_c" in row.keys() else None,
                    row["temp_f"] if "temp_f" in row.keys() else None,
                    row["sample_status"] if "sample_status" in row.keys() else None,
                    row["sample_detail"] if "sample_detail" in row.keys() else None,
                    now,
                ),
            )

    for snapshot in list_recent_snapshots(limit=5000):
        captured_at = parse_snapshot_captured_at(snapshot)
        if captured_at is None:
            continue
        captured_iso = captured_at.isoformat()
        if captured_iso < started_at_utc or captured_iso > sync_cutoff:
            continue
        connection.execute(
            """
            INSERT INTO firing_log_snapshots (
                firing_log_id,
                filename,
                captured_at_utc,
                source_type,
                original_filename,
                caption,
                created_at
            ) VALUES (?, ?, ?, 'AUTO', ?, '', ?)
            """,
            (
                firing_log_id,
                snapshot["filename"],
                captured_iso,
                snapshot["filename"],
                now,
            ),
        )


def fetch_firing_logs(limit: int = 100) -> dict:
    connection = open_readonly_connection()
    if connection is None or not table_exists(connection, "firing_logs"):
        if connection is not None:
            connection.close()
        return {"logs": []}

    try:
        rows = connection.execute(
            """
            SELECT
                id,
                title,
                firing_type,
                planned_cone,
                started_at_utc,
                ended_at_utc,
                description,
                result_summary,
                result_status,
                post_mortem,
                created_at,
                updated_at,
                (SELECT COUNT(*) FROM firing_log_events WHERE firing_log_id = firing_logs.id) AS event_count,
                (SELECT COUNT(*) FROM firing_log_snapshots WHERE firing_log_id = firing_logs.id) AS snapshot_count
            FROM firing_logs
            ORDER BY started_at_utc DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        connection.close()

    return {
        "logs": [
            {
                "id": row["id"],
                "title": row["title"],
                "firing_type": row["firing_type"],
                "planned_cone": row["planned_cone"],
                "started_at_utc": row["started_at_utc"],
                "ended_at_utc": row["ended_at_utc"],
                "description": row["description"],
                "result_summary": row["result_summary"],
                "result_status": row["result_status"],
                "post_mortem": row["post_mortem"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "event_count": int(row["event_count"] or 0),
                "snapshot_count": int(row["snapshot_count"] or 0),
            }
            for row in rows
        ]
    }


def fetch_firing_log_detail(firing_log_id: int) -> dict:
    connection = open_readonly_connection()
    if connection is None or not table_exists(connection, "firing_logs"):
        if connection is not None:
            connection.close()
        raise ValueError("firing log not found")

    try:
        row = connection.execute(
            """
            SELECT
                id,
                title,
                firing_type,
                planned_cone,
                started_at_utc,
                ended_at_utc,
                description,
                result_summary,
                result_status,
                post_mortem,
                created_at,
                updated_at,
                (SELECT COUNT(*) FROM firing_log_events WHERE firing_log_id = firing_logs.id) AS event_count,
                (SELECT COUNT(*) FROM firing_log_snapshots WHERE firing_log_id = firing_logs.id) AS snapshot_count
            FROM firing_logs
            WHERE id = ?
            """,
            (firing_log_id,),
        ).fetchone()
        if row is None:
            raise ValueError("firing log not found")

        event_rows = connection.execute(
            """
            SELECT
                id,
                event_id,
                timestamp_utc,
                event_type,
                label,
                detail,
                temp_c,
                temp_f,
                sample_status,
                sample_detail
            FROM firing_log_events
            WHERE firing_log_id = ?
            ORDER BY timestamp_utc ASC, id ASC
            """,
            (firing_log_id,),
        ).fetchall()
        snapshot_rows = connection.execute(
            """
            SELECT id, filename, captured_at_utc, source_type, original_filename, caption
            FROM firing_log_snapshots
            WHERE firing_log_id = ?
            ORDER BY captured_at_utc ASC, id ASC
            """,
            (firing_log_id,),
        ).fetchall()
    finally:
        connection.close()

    return {
        "log": {
            "id": row["id"],
            "title": row["title"],
            "firing_type": row["firing_type"],
            "planned_cone": row["planned_cone"],
            "started_at_utc": row["started_at_utc"],
            "ended_at_utc": row["ended_at_utc"],
            "description": row["description"],
            "result_summary": row["result_summary"],
            "result_status": row["result_status"],
            "post_mortem": row["post_mortem"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "event_count": int(row["event_count"] or 0),
            "snapshot_count": int(row["snapshot_count"] or 0),
        },
        "events": [
            {
                "id": event_row["id"],
                "event_id": event_row["event_id"],
                "timestamp_utc": event_row["timestamp_utc"],
                "event_type": event_row["event_type"],
                "label": event_row["label"],
                "detail": event_row["detail"],
                "temp_c": event_row["temp_c"],
                "temp_f": event_row["temp_f"],
                "sample_status": event_row["sample_status"],
                "sample_detail": event_row["sample_detail"],
            }
            for event_row in event_rows
        ],
        "snapshots": [
            {
                "id": snapshot_row["id"],
                "filename": snapshot_row["filename"],
                "captured_at_utc": snapshot_row["captured_at_utc"],
                "source_type": snapshot_row["source_type"] if "source_type" in snapshot_row.keys() else "AUTO",
                "original_filename": snapshot_row["original_filename"] if "original_filename" in snapshot_row.keys() else None,
                "caption": snapshot_row["caption"],
                "url": (
                    f"/firing-log-photos/{snapshot_row['filename']}"
                    if (snapshot_row["source_type"] if "source_type" in snapshot_row.keys() else "AUTO") == "RESULT"
                    else f"/camera/archive/{snapshot_row['filename']}"
                ),
            }
            for snapshot_row in snapshot_rows
        ],
    }


def create_firing_log(payload: dict) -> dict:
    parsed = parse_firing_log_payload(payload)
    now = datetime.now(timezone.utc).isoformat()
    connection = open_readwrite_connection()
    try:
        cursor = connection.execute(
            """
            INSERT INTO firing_logs (
                title,
                firing_type,
                planned_cone,
                started_at_utc,
                ended_at_utc,
                description,
                result_summary,
                result_status,
                post_mortem,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parsed["title"],
                parsed["firing_type"],
                parsed["planned_cone"],
                parsed["started_at_utc"],
                parsed["ended_at_utc"],
                parsed["description"],
                parsed["result_summary"],
                parsed["result_status"],
                parsed["post_mortem"],
                now,
                now,
            ),
        )
        firing_log_id = int(cursor.lastrowid)
        sync_firing_log_related_data(
            connection,
            firing_log_id,
            started_at_utc=parsed["started_at_utc"],
            ended_at_utc=parsed["ended_at_utc"],
        )
        connection.commit()
    finally:
        connection.close()
    return {"ok": True, "id": firing_log_id}


def update_firing_log(firing_log_id: int, payload: dict) -> dict:
    parsed = parse_firing_log_payload(payload)
    now = datetime.now(timezone.utc).isoformat()
    connection = open_readwrite_connection()
    try:
        current = connection.execute(
            "SELECT id FROM firing_logs WHERE id = ?",
            (firing_log_id,),
        ).fetchone()
        if current is None:
            raise ValueError("firing log not found")
        connection.execute(
            """
            UPDATE firing_logs
            SET title = ?, firing_type = ?, planned_cone = ?, started_at_utc = ?, ended_at_utc = ?,
                description = ?, result_summary = ?, result_status = ?, post_mortem = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                parsed["title"],
                parsed["firing_type"],
                parsed["planned_cone"],
                parsed["started_at_utc"],
                parsed["ended_at_utc"],
                parsed["description"],
                parsed["result_summary"],
                parsed["result_status"],
                parsed["post_mortem"],
                now,
                firing_log_id,
            ),
        )
        sync_firing_log_related_data(
            connection,
            firing_log_id,
            started_at_utc=parsed["started_at_utc"],
            ended_at_utc=parsed["ended_at_utc"],
        )
        connection.commit()
    finally:
        connection.close()
    return {"ok": True, "id": firing_log_id}


def refresh_firing_log_related_data(firing_log_id: int) -> dict:
    connection = open_readwrite_connection()
    try:
        row = connection.execute(
            "SELECT started_at_utc, ended_at_utc FROM firing_logs WHERE id = ?",
            (firing_log_id,),
        ).fetchone()
        if row is None:
            raise ValueError("firing log not found")
        sync_firing_log_related_data(
            connection,
            firing_log_id,
            started_at_utc=row["started_at_utc"],
            ended_at_utc=row["ended_at_utc"],
        )
        connection.execute(
            "UPDATE firing_logs SET updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), firing_log_id),
        )
        connection.commit()
    finally:
        connection.close()
    return {"ok": True, "id": firing_log_id}


def upload_firing_log_photo(firing_log_id: int, payload: dict) -> dict:
    content_base64 = str(payload.get("content_base64", "")).strip()
    original_filename = sanitize_uploaded_photo_name(str(payload.get("filename", "")).strip())
    caption = str(payload.get("caption", "")).strip()
    if not content_base64:
        raise ValueError("photo content is required")
    if not original_filename:
        raise ValueError("photo filename is required")

    try:
        image_bytes = base64.b64decode(content_base64, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("photo upload was not valid base64") from exc

    if not image_bytes:
        raise ValueError("photo upload was empty")

    lowered_name = original_filename.lower()
    extension = Path(lowered_name).suffix
    if extension not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise ValueError("photo must be .jpg, .jpeg, .png, or .webp")

    mime_type = str(payload.get("mime_type", "")).strip().lower()
    if mime_type and mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise ValueError("unsupported image mime type")

    FIRING_LOG_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp_text = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stored_filename = f"log-{firing_log_id}-{timestamp_text}-{original_filename}"
    photo_path = FIRING_LOG_PHOTOS_DIR / stored_filename
    photo_path.write_bytes(image_bytes)

    now = datetime.now(timezone.utc).isoformat()
    connection = open_readwrite_connection()
    try:
        row = connection.execute(
            "SELECT id FROM firing_logs WHERE id = ?",
            (firing_log_id,),
        ).fetchone()
        if row is None:
            raise ValueError("firing log not found")
        connection.execute(
            """
            INSERT INTO firing_log_snapshots (
                firing_log_id,
                filename,
                captured_at_utc,
                source_type,
                original_filename,
                caption,
                created_at
            ) VALUES (?, ?, ?, 'RESULT', ?, ?, ?)
            """,
            (
                firing_log_id,
                stored_filename,
                now,
                original_filename,
                caption,
                now,
            ),
        )
        connection.execute(
            "UPDATE firing_logs SET updated_at = ? WHERE id = ?",
            (now, firing_log_id),
        )
        connection.commit()
    finally:
        connection.close()

    return {"ok": True, "filename": stored_filename}


def build_firing_log_markdown(firing_log_id: int) -> str:
    detail = fetch_firing_log_detail(firing_log_id)
    log = detail["log"]
    events = detail["events"]
    snapshots = detail["snapshots"]
    lines = [
        f"# {log['title']}",
        "",
        f"- Type: {log['firing_type']}",
        f"- Planned cone: {log['planned_cone'] or 'n/a'}",
        f"- Result: {log['result_status']}",
        f"- Start: {log['started_at_utc']}",
        f"- End: {log['ended_at_utc'] or 'in progress'}",
        f"- Linked events: {log['event_count']}",
        f"- Linked photos: {log['snapshot_count']}",
        "",
        "## Description",
        "",
        log["description"] or "None recorded.",
        "",
        "## Results",
        "",
        log["result_summary"] or "None recorded.",
        "",
        "## Post-Mortem",
        "",
        log["post_mortem"] or "None recorded.",
        "",
        "## Events",
        "",
    ]
    if events:
        for event in events:
            state_parts = []
            if event["temp_f"] is not None:
                state_parts.append(f"{float(event['temp_f']):.1f} F")
            if event["sample_status"]:
                state_parts.append(event["sample_status"])
            state_text = f" ({', '.join(state_parts)})" if state_parts else ""
            detail_text = f": {event['detail']}" if event["detail"] else ""
            lines.append(f"- {event['timestamp_utc']} [{event['event_type']}] {event['label']}{state_text}{detail_text}")
    else:
        lines.append("- None recorded.")
    lines.extend(["", "## Photos", ""])
    if snapshots:
        for snapshot in snapshots:
            source_label = "Result photo" if snapshot.get("source_type") == "RESULT" else "Kiln snapshot"
            caption_text = f" - {snapshot['caption']}" if snapshot.get("caption") else ""
            url_text = snapshot.get("url") or ""
            lines.append(
                f"- {source_label} @ {snapshot['captured_at_utc'] or 'unknown time'}: "
                f"{snapshot.get('original_filename') or snapshot['filename']}{caption_text}"
                f"{f' ({url_text})' if url_text else ''}"
            )
    else:
        lines.append("- None recorded.")
    lines.append("")
    return "\n".join(lines)


def fetch_events(
    limit: int = 100,
    *,
    event_type: str | None = None,
    search: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    connection = open_readonly_connection()
    if connection is None or not table_exists(connection, "kiln_events"):
        if connection is not None:
            connection.close()
        return {"events": []}

    try:
        select_fields = "id, timestamp_utc, event_type, label, detail"
        if table_has_column(connection, "kiln_events", "temp_c"):
            select_fields += ", temp_c"
        if table_has_column(connection, "kiln_events", "temp_f"):
            select_fields += ", temp_f"
        if table_has_column(connection, "kiln_events", "sample_status"):
            select_fields += ", sample_status"
        if table_has_column(connection, "kiln_events", "sample_detail"):
            select_fields += ", sample_detail"
        clauses = []
        params: list[object] = []
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type.strip().upper())
        if search:
            clauses.append("(label LIKE ? OR detail LIKE ?)")
            like_value = f"%{search.strip()}%"
            params.extend([like_value, like_value])
        if start:
            clauses.append("timestamp_utc >= ?")
            params.append(start)
        if end:
            clauses.append("timestamp_utc <= ?")
            params.append(end)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            f"""
            SELECT {select_fields}
            FROM kiln_events
            {where_sql}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    finally:
        connection.close()

    return {
        "events": [
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
                "sample_age": format_sample_age(row["timestamp_utc"]),
            }
            for row in rows
        ]
    }


def fetch_faults(
    window_name: str = "24h",
    limit: int = 100,
    *,
    search: str | None = None,
    min_temp_f: float | None = None,
    max_temp_f: float | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    if window_name not in HISTORY_WINDOWS:
        window_name = "24h"

    connection = open_readonly_connection()
    if connection is None:
        return {"range": window_name, "faults": [], "diagnostics": None}

    cutoff = (datetime.now(timezone.utc) - HISTORY_WINDOWS[window_name]).isoformat()
    try:
        select_fields = "id, timestamp_utc, temp_c, temp_f, detail"
        if table_has_column(connection, "temperature_log", "cold_junction_c"):
            select_fields += ", cold_junction_c"
        if table_has_column(connection, "temperature_log", "sensor_model"):
            select_fields += ", sensor_model"
        if table_has_column(connection, "temperature_log", "thermocouple_type"):
            select_fields += ", thermocouple_type"
        if table_has_column(connection, "temperature_log", "previous_good_temp_c"):
            select_fields += ", previous_good_temp_c"
        if table_has_column(connection, "temperature_log", "previous_good_temp_f"):
            select_fields += ", previous_good_temp_f"
        if table_has_column(connection, "temperature_log", "delta_from_previous_good_c"):
            select_fields += ", delta_from_previous_good_c"
        if table_has_column(connection, "temperature_log", "delta_from_previous_good_f"):
            select_fields += ", delta_from_previous_good_f"
        if table_has_column(connection, "temperature_log", "error_streak"):
            select_fields += ", error_streak"
        if table_has_column(connection, "temperature_log", "seconds_since_last_good"):
            select_fields += ", seconds_since_last_good"
        if table_has_column(connection, "temperature_log", "last_good_timestamp_utc"):
            select_fields += ", last_good_timestamp_utc"
        if table_has_column(connection, "temperature_log", "raw_frame_hex"):
            select_fields += ", raw_frame_hex"
        if table_has_column(connection, "temperature_log", "fault_bits_hex"):
            select_fields += ", fault_bits_hex"
        if table_has_column(connection, "temperature_log", "fault_flags"):
            select_fields += ", fault_flags"
        clauses = ["status = 'ERROR'"]
        params: list[object] = []
        effective_start = start or cutoff
        if effective_start:
            clauses.append("timestamp_utc >= ?")
            params.append(effective_start)
        if end:
            clauses.append("timestamp_utc <= ?")
            params.append(end)
        if search:
            clauses.append("detail LIKE ?")
            params.append(f"%{search.strip()}%")
        if min_temp_f is not None:
            clauses.append("temp_f >= ?")
            params.append(min_temp_f)
        if max_temp_f is not None:
            clauses.append("temp_f <= ?")
            params.append(max_temp_f)
        where_sql = " AND ".join(clauses)
        rows = connection.execute(
            f"""
            SELECT {select_fields}
            FROM temperature_log
            WHERE {where_sql}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        diagnostics = build_fault_diagnostics(connection, effective_start)
    finally:
        connection.close()

    return {
        "range": window_name,
        "faults": [
            {
                "id": row["id"],
                "timestamp_utc": row["timestamp_utc"],
                "temp_c": row["temp_c"],
                "temp_f": row["temp_f"],
                "detail": row["detail"],
                "cold_junction_c": row["cold_junction_c"] if "cold_junction_c" in row.keys() else None,
                "sensor_model": row["sensor_model"] if "sensor_model" in row.keys() else None,
                "thermocouple_type": row["thermocouple_type"] if "thermocouple_type" in row.keys() else None,
                "previous_good_temp_c": row["previous_good_temp_c"] if "previous_good_temp_c" in row.keys() else None,
                "previous_good_temp_f": row["previous_good_temp_f"] if "previous_good_temp_f" in row.keys() else None,
                "delta_from_previous_good_c": row["delta_from_previous_good_c"] if "delta_from_previous_good_c" in row.keys() else None,
                "delta_from_previous_good_f": row["delta_from_previous_good_f"] if "delta_from_previous_good_f" in row.keys() else None,
                "error_streak": row["error_streak"] if "error_streak" in row.keys() else None,
                "seconds_since_last_good": row["seconds_since_last_good"] if "seconds_since_last_good" in row.keys() else None,
                "last_good_timestamp_utc": row["last_good_timestamp_utc"] if "last_good_timestamp_utc" in row.keys() else None,
                "raw_frame_hex": row["raw_frame_hex"] if "raw_frame_hex" in row.keys() else None,
                "fault_bits_hex": row["fault_bits_hex"] if "fault_bits_hex" in row.keys() else None,
                "fault_flags": row["fault_flags"] if "fault_flags" in row.keys() else None,
                "sample_age": format_sample_age(row["timestamp_utc"]),
            }
            for row in rows
        ],
        "diagnostics": diagnostics,
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
        latest_sample = connection.execute(
            """
            SELECT temp_c, temp_f, status, detail
            FROM temperature_log
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        connection.execute(
            """
            INSERT INTO kiln_events (
                timestamp_utc,
                event_type,
                label,
                detail,
                temp_c,
                temp_f,
                sample_status,
                sample_detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp_utc,
                event_type,
                label,
                detail,
                latest_sample["temp_c"] if latest_sample is not None and "temp_c" in latest_sample.keys() else None,
                latest_sample["temp_f"] if latest_sample is not None and "temp_f" in latest_sample.keys() else None,
                latest_sample["status"] if latest_sample is not None and "status" in latest_sample.keys() else None,
                latest_sample["detail"] if latest_sample is not None and "detail" in latest_sample.keys() else None,
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

        static_path = resolve_static_path(parsed_path.path)
        if static_path is not None:
            self.send_file_response(static_path)
            return

        if parsed_path.path == "/":
            self.send_text_response(DASHBOARD_PAGE_HTML, content_type="text/html; charset=utf-8")
            return

        if parsed_path.path == "/logs":
            self.send_text_response(FIRING_LOGS_PAGE_HTML, content_type="text/html; charset=utf-8")
            return

        if parsed_path.path == "/alerts":
            self.send_text_response(ALERTS_PAGE_HTML, content_type="text/html; charset=utf-8")
            return

        if parsed_path.path == "/panel":
            self.send_text_response(PANEL_PAGE_HTML, content_type="text/html; charset=utf-8")
            return

        if parsed_path.path == "/events":
            self.send_text_response(EVENTS_PAGE_HTML, content_type="text/html; charset=utf-8")
            return

        if parsed_path.path == "/faults":
            self.send_text_response(FAULTS_PAGE_HTML, content_type="text/html; charset=utf-8")
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

        if parsed_path.path.startswith("/camera/archive/"):
            filename = unquote(parsed_path.path.removeprefix("/camera/archive/"))
            if not filename or Path(filename).name != filename:
                self.send_error(400, "Invalid snapshot filename")
                return
            archived_path = CAMERA_SNAPSHOTS_DIR / filename
            if not archived_path.exists():
                self.send_error(404, "Snapshot not found")
                return
            try:
                image_bytes = archived_path.read_bytes()
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

        if parsed_path.path.startswith("/firing-log-photos/"):
            filename = unquote(parsed_path.path.removeprefix("/firing-log-photos/"))
            if not filename or Path(filename).name != filename:
                self.send_error(400, "Invalid firing log photo filename")
                return
            photo_path = FIRING_LOG_PHOTOS_DIR / filename
            if not photo_path.exists():
                self.send_error(404, "Firing log photo not found")
                return
            try:
                image_bytes = photo_path.read_bytes()
            except OSError:
                self.send_error(500, "Unable to read firing log photo")
                return
            mime_type = "image/jpeg"
            if photo_path.suffix.lower() == ".png":
                mime_type = "image/png"
            elif photo_path.suffix.lower() == ".webp":
                mime_type = "image/webp"
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
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
            start_text = query.get("start", [""])[0].strip()
            end_text = query.get("end", [""])[0].strip()
            if start_text and end_text:
                try:
                    start_dt = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
                    end_dt = datetime.fromisoformat(end_text.replace("Z", "+00:00"))
                except ValueError:
                    self.send_json_response({"error": "Invalid start or end timestamp."}, status=400)
                    return
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                self.send_json_response(fetch_history_between(start_dt, end_dt, resolution_name))
            else:
                self.send_json_response(fetch_history_with_resolution(range_name, resolution_name))
            return

        if parsed_path.path == "/api/alert-rules":
            self.send_json_response(fetch_alert_rules())
            return

        if parsed_path.path == "/api/alert-deliveries":
            self.send_json_response(fetch_alert_deliveries())
            return

        if parsed_path.path == "/api/recent-alerts":
            self.send_json_response(fetch_recent_alerts())
            return

        if parsed_path.path == "/api/firing-logs":
            query = parse_qs(parsed_path.query)
            limit = int(query.get("limit", ["100"])[0])
            self.send_json_response(fetch_firing_logs(limit=max(1, min(limit, 500))))
            return

        if parsed_path.path.startswith("/api/firing-logs/") and parsed_path.path.endswith("/export.md"):
            firing_log_id = int(parsed_path.path.split("/")[3])
            markdown_body = build_firing_log_markdown(firing_log_id)
            self.send_text_response(markdown_body, content_type="text/markdown; charset=utf-8")
            return

        if parsed_path.path.startswith("/api/firing-logs/"):
            firing_log_id = int(parsed_path.path.split("/")[3])
            self.send_json_response(fetch_firing_log_detail(firing_log_id))
            return

        if parsed_path.path == "/api/events":
            query = parse_qs(parsed_path.query)
            limit = int(query.get("limit", ["100"])[0])
            event_type = query.get("event_type", [""])[0].strip() or None
            search = query.get("search", [""])[0].strip() or None
            start = query.get("start", [""])[0].strip() or None
            end = query.get("end", [""])[0].strip() or None
            self.send_json_response(
                fetch_events(
                    limit=max(1, min(limit, 500)),
                    event_type=event_type,
                    search=search,
                    start=start,
                    end=end,
                )
            )
            return

        if parsed_path.path == "/api/faults":
            query = parse_qs(parsed_path.query)
            range_name = query.get("range", ["24h"])[0]
            limit = int(query.get("limit", ["100"])[0])
            search = query.get("search", [""])[0].strip() or None
            start = query.get("start", [""])[0].strip() or None
            end = query.get("end", [""])[0].strip() or None
            min_temp_text = query.get("min_temp_f", [""])[0].strip()
            max_temp_text = query.get("max_temp_f", [""])[0].strip()
            min_temp_f = float(min_temp_text) if min_temp_text else None
            max_temp_f = float(max_temp_text) if max_temp_text else None
            self.send_json_response(
                fetch_faults(
                    range_name,
                    limit=max(1, min(limit, 500)),
                    search=search,
                    min_temp_f=min_temp_f,
                    max_temp_f=max_temp_f,
                    start=start,
                    end=end,
                )
            )
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

            if parsed_path.path == "/api/firing-logs":
                self.send_json_response(create_firing_log(payload))
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

            if parsed_path.path.startswith("/api/firing-logs/") and parsed_path.path.endswith("/refresh"):
                firing_log_id = int(parsed_path.path.split("/")[3])
                self.send_json_response(refresh_firing_log_related_data(firing_log_id))
                return

            if parsed_path.path.startswith("/api/firing-logs/") and parsed_path.path.endswith("/photos"):
                firing_log_id = int(parsed_path.path.split("/")[3])
                self.send_json_response(upload_firing_log_photo(firing_log_id, payload))
                return

            if parsed_path.path.startswith("/api/alert-rules/"):
                rule_id = int(parsed_path.path.split("/")[3])
                self.send_json_response(update_alert_rule(rule_id, payload))
                return

            if parsed_path.path.startswith("/api/firing-logs/"):
                firing_log_id = int(parsed_path.path.split("/")[3])
                self.send_json_response(update_firing_log(firing_log_id, payload))
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

    def send_file_response(self, file_path: Path) -> None:
        try:
            body = file_path.read_bytes()
        except OSError:
            self.send_error(500, "Unable to read static asset")
            return

        content_type, _ = mimetypes.guess_type(str(file_path))
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
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
