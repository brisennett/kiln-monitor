from __future__ import annotations

from dashboard_service_core import *

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

