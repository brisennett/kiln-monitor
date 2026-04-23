from __future__ import annotations

from dashboard_service_core import *

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

