from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import board


class SensorReadError(Exception):
    """Raised when the thermocouple cannot be read successfully."""


@dataclass
class TemperatureSample:
    timestamp: datetime
    temp_c: Optional[float]
    temp_f: Optional[float]
    status: str
    detail: str = ""
    cold_junction_c: Optional[float] = None


def resolve_board_pin(pin_name: str):
    try:
        return getattr(board, pin_name)
    except AttributeError as exc:
        raise ValueError(f"Unsupported board pin for sensor chip select: {pin_name}") from exc
