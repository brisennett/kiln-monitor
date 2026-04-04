from __future__ import annotations

import sqlite3
from pathlib import Path

from config import SQLITE_SYNCHRONOUS_MODE
from sensor.common import TemperatureSample


class SQLiteLogger:
    """Simple SQLite logger configured for durable, low-frequency writes."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._db_path)
        self._connection.execute("PRAGMA journal_mode=WAL;")
        self._connection.execute(f"PRAGMA synchronous={self._validate_synchronous_mode()};")
        self._connection.execute("PRAGMA busy_timeout=5000;")
        self._create_tables()

    def _create_tables(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS temperature_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                temp_c REAL,
                temp_f REAL,
                status TEXT NOT NULL,
                detail TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def log_sample(self, sample: TemperatureSample) -> None:
        self._connection.execute(
            """
            INSERT INTO temperature_log (
                timestamp_utc,
                temp_c,
                temp_f,
                status,
                detail
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                sample.timestamp.isoformat(),
                sample.temp_c,
                sample.temp_f,
                sample.status,
                sample.detail,
            ),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def _validate_synchronous_mode() -> str:
        allowed_modes = {"OFF", "NORMAL", "FULL", "EXTRA"}
        if SQLITE_SYNCHRONOUS_MODE not in allowed_modes:
            raise ValueError(
                f"Unsupported SQLite synchronous mode: {SQLITE_SYNCHRONOUS_MODE}. "
                f"Expected one of {sorted(allowed_modes)}."
            )
        return SQLITE_SYNCHRONOUS_MODE
