from __future__ import annotations

from dashboard_service_core import *
from dashboard_service_profiles import fetch_active_profile_run

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

