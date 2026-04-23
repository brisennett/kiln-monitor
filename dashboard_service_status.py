from __future__ import annotations

from dashboard_service_core import *
from dashboard_service_profiles import fetch_active_profile_run

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
