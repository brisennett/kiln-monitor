from __future__ import annotations

import sqlite3
from pathlib import Path

from config import SQLITE_SYNCHRONOUS_MODE
from alerts import AlertEvent, AlertRule
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
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_temperature_log_timestamp_utc
            ON temperature_log(timestamp_utc)
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_temperature_log_status
            ON temperature_log(status)
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                level TEXT NOT NULL,
                kind TEXT NOT NULL,
                detail TEXT NOT NULL,
                temp_c REAL,
                temp_f REAL,
                rule_id INTEGER,
                rule_name TEXT
            )
            """
        )
        self._ensure_column("alert_log", "rule_id", "INTEGER")
        self._ensure_column("alert_log", "rule_name", "TEXT")
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_alert_log_timestamp_utc
            ON alert_log(timestamp_utc)
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                rule_type TEXT NOT NULL,
                threshold_f REAL NOT NULL,
                severity TEXT NOT NULL,
                hysteresis_f REAL NOT NULL DEFAULT 5.0,
                color_hex TEXT NOT NULL DEFAULT '#38bdf8',
                active INTEGER NOT NULL DEFAULT 0,
                last_triggered_at TEXT
            )
            """
        )
        self._ensure_column("alert_rules", "color_hex", "TEXT NOT NULL DEFAULT '#38bdf8'")
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_alert_rules_enabled
            ON alert_rules(enabled)
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

    def log_alert(self, alert: AlertEvent) -> None:
        self._connection.execute(
            """
            INSERT INTO alert_log (
                timestamp_utc,
                level,
                kind,
                detail,
                temp_c,
                temp_f,
                rule_id,
                rule_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert.timestamp_utc,
                alert.level,
                alert.kind,
                alert.detail,
                alert.temp_c,
                alert.temp_f,
                alert.rule_id,
                alert.rule_name,
            ),
        )
        self._connection.commit()

    def fetch_alert_rules(self) -> list[AlertRule]:
        rows = self._connection.execute(
            """
            SELECT id, name, enabled, rule_type, threshold_f, severity, hysteresis_f, color_hex, active, last_triggered_at
            FROM alert_rules
            ORDER BY threshold_f ASC, id ASC
            """
        ).fetchall()
        return [
            AlertRule(
                id=row[0],
                name=row[1],
                enabled=bool(row[2]),
                rule_type=row[3],
                threshold_f=row[4],
                severity=row[5],
                hysteresis_f=row[6],
                color_hex=row[7],
                active=bool(row[8]),
                last_triggered_at=row[9],
            )
            for row in rows
        ]

    def update_alert_rule_state(self, rule: AlertRule) -> None:
        self._connection.execute(
            """
            UPDATE alert_rules
            SET active = ?, last_triggered_at = ?
            WHERE id = ?
            """,
            (
                int(rule.active),
                rule.last_triggered_at,
                rule.id,
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

    def _ensure_column(self, table_name: str, column_name: str, column_type: str) -> None:
        columns = self._connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing_names = {column[1] for column in columns}
        if column_name in existing_names:
            return
        self._connection.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
        )
