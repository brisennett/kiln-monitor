from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import board


class SensorReadError(Exception):
    """Raised when the thermocouple cannot be read successfully."""

    def __init__(
        self,
        detail: str,
        *,
        raw_frame_hex: Optional[str] = None,
        fault_bits_hex: Optional[str] = None,
        fault_flags: Optional[str] = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.raw_frame_hex = raw_frame_hex
        self.fault_bits_hex = fault_bits_hex
        self.fault_flags = fault_flags


@dataclass
class TemperatureSample:
    timestamp: datetime
    temp_c: Optional[float]
    temp_f: Optional[float]
    status: str
    detail: str = ""
    cold_junction_c: Optional[float] = None
    sensor_model: Optional[str] = None
    thermocouple_type: Optional[str] = None
    previous_good_temp_c: Optional[float] = None
    previous_good_temp_f: Optional[float] = None
    delta_from_previous_good_c: Optional[float] = None
    delta_from_previous_good_f: Optional[float] = None
    error_streak: Optional[int] = None
    seconds_since_last_good: Optional[float] = None
    last_good_timestamp_utc: Optional[str] = None
    raw_frame_hex: Optional[str] = None
    fault_bits_hex: Optional[str] = None
    fault_flags: Optional[str] = None


def resolve_board_pin(pin_name: str):
    try:
        return getattr(board, pin_name)
    except AttributeError as exc:
        raise ValueError(f"Unsupported board pin for sensor chip select: {pin_name}") from exc
