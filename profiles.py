from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class ProfileSegment:
    name: str
    target_temp_c: float
    ramp_rate_c_per_hour: float
    soak_minutes: float = 0.0


@dataclass
class FiringProfile:
    id: int | None
    name: str
    description: str
    cone: str
    segments: list[ProfileSegment]
    created_at: str | None = None
    updated_at: str | None = None


def validate_profile(profile: FiringProfile) -> None:
    if not profile.name.strip():
        raise ValueError("profile name is required")
    if not profile.segments:
        raise ValueError("at least one profile segment is required")

    for index, segment in enumerate(profile.segments, start=1):
        if not segment.name.strip():
            raise ValueError(f"segment {index} name is required")
        if segment.ramp_rate_c_per_hour <= 0:
            raise ValueError(f"segment {index} ramp rate must be greater than 0")
        if segment.soak_minutes < 0:
            raise ValueError(f"segment {index} soak minutes must be zero or greater")


def segment_to_payload(segment: ProfileSegment) -> dict:
    return {
        "name": segment.name,
        "target_temp_c": segment.target_temp_c,
        "target_temp_f": c_to_f(segment.target_temp_c),
        "ramp_rate_c_per_hour": segment.ramp_rate_c_per_hour,
        "ramp_rate_f_per_hour": segment.ramp_rate_c_per_hour * 9.0 / 5.0,
        "soak_minutes": segment.soak_minutes,
    }


def profile_to_payload(profile: FiringProfile) -> dict:
    total_duration_seconds = profile_total_duration_seconds(profile, None)
    return {
        "id": profile.id,
        "name": profile.name,
        "description": profile.description,
        "cone": profile.cone,
        "segments": [segment_to_payload(segment) for segment in profile.segments],
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
        "total_duration_seconds": total_duration_seconds,
        "total_duration_hours": total_duration_seconds / 3600.0,
    }


def c_to_f(temp_c: float) -> float:
    return (temp_c * 9.0 / 5.0) + 32.0


def profile_total_duration_seconds(
    profile: FiringProfile,
    start_temp_c: float | None,
) -> float:
    current_temp_c = start_temp_c
    elapsed_seconds = 0.0

    for segment in profile.segments:
        if current_temp_c is not None:
            ramp_delta = abs(segment.target_temp_c - current_temp_c)
            elapsed_seconds += (ramp_delta / segment.ramp_rate_c_per_hour) * 3600.0
        elapsed_seconds += segment.soak_minutes * 60.0
        current_temp_c = segment.target_temp_c

    return elapsed_seconds


def expected_profile_state(
    profile: FiringProfile,
    started_at: datetime,
    start_temp_c: float,
    at_time: datetime,
) -> dict:
    validate_profile(profile)

    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if at_time.tzinfo is None:
        at_time = at_time.replace(tzinfo=timezone.utc)

    elapsed_seconds = max(0.0, (at_time - started_at).total_seconds())
    current_temp_c = start_temp_c
    cursor_seconds = 0.0

    for index, segment in enumerate(profile.segments):
        ramp_delta = segment.target_temp_c - current_temp_c
        ramp_seconds = abs(ramp_delta / segment.ramp_rate_c_per_hour) * 3600.0
        if elapsed_seconds <= cursor_seconds + ramp_seconds:
            progress = 1.0 if ramp_seconds == 0 else (elapsed_seconds - cursor_seconds) / ramp_seconds
            expected_temp_c = current_temp_c + (ramp_delta * progress)
            return {
                "phase": "RAMP",
                "segment_index": index,
                "segment_name": segment.name,
                "expected_temp_c": expected_temp_c,
                "expected_temp_f": c_to_f(expected_temp_c),
                "elapsed_seconds": elapsed_seconds,
                "segment_elapsed_seconds": max(0.0, elapsed_seconds - cursor_seconds),
                "segment_total_seconds": ramp_seconds,
                "complete": False,
            }

        cursor_seconds += ramp_seconds
        current_temp_c = segment.target_temp_c
        soak_seconds = segment.soak_minutes * 60.0
        if elapsed_seconds <= cursor_seconds + soak_seconds:
            return {
                "phase": "SOAK",
                "segment_index": index,
                "segment_name": segment.name,
                "expected_temp_c": current_temp_c,
                "expected_temp_f": c_to_f(current_temp_c),
                "elapsed_seconds": elapsed_seconds,
                "segment_elapsed_seconds": max(0.0, elapsed_seconds - cursor_seconds),
                "segment_total_seconds": soak_seconds,
                "complete": False,
            }
        cursor_seconds += soak_seconds

    return {
        "phase": "COMPLETE",
        "segment_index": len(profile.segments) - 1,
        "segment_name": profile.segments[-1].name,
        "expected_temp_c": current_temp_c,
        "expected_temp_f": c_to_f(current_temp_c),
        "elapsed_seconds": elapsed_seconds,
        "segment_elapsed_seconds": 0.0,
        "segment_total_seconds": 0.0,
        "complete": True,
    }


def generate_profile_overlay(
    profile: FiringProfile,
    started_at: datetime,
    start_temp_c: float,
    window_start: datetime,
    window_end: datetime,
    step_seconds: int,
) -> list[dict]:
    validate_profile(profile)

    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)

    total_duration_seconds = profile_total_duration_seconds(profile, start_temp_c)
    profile_end = started_at + timedelta(seconds=total_duration_seconds)
    query_start = max(window_start, started_at)
    query_end = min(window_end, profile_end)

    if query_end < query_start:
        return []

    overlay: list[dict] = []
    cursor = query_start
    while cursor <= query_end:
        state = expected_profile_state(profile, started_at, start_temp_c, cursor)
        overlay.append(
            {
                "timestamp_utc": cursor.isoformat(),
                "temp_c": state["expected_temp_c"],
                "temp_f": state["expected_temp_f"],
                "segment_name": state["segment_name"],
                "phase": state["phase"],
            }
        )
        cursor += timedelta(seconds=max(1, step_seconds))

    if not overlay or overlay[-1]["timestamp_utc"] != query_end.isoformat():
        state = expected_profile_state(profile, started_at, start_temp_c, query_end)
        overlay.append(
            {
                "timestamp_utc": query_end.isoformat(),
                "temp_c": state["expected_temp_c"],
                "temp_f": state["expected_temp_f"],
                "segment_name": state["segment_name"],
                "phase": state["phase"],
            }
        )

    return overlay
