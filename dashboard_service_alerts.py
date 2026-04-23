from __future__ import annotations

from dashboard_service_core import *

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

