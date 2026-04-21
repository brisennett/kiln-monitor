from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
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
                detail TEXT NOT NULL,
                cold_junction_c REAL,
                sensor_model TEXT,
                thermocouple_type TEXT,
                previous_good_temp_c REAL,
                previous_good_temp_f REAL,
                delta_from_previous_good_c REAL,
                delta_from_previous_good_f REAL,
                error_streak INTEGER,
                seconds_since_last_good REAL,
                last_good_timestamp_utc TEXT,
                raw_frame_hex TEXT,
                fault_bits_hex TEXT,
                fault_flags TEXT
            )
            """
        )
        self._ensure_column("temperature_log", "cold_junction_c", "REAL")
        self._ensure_column("temperature_log", "sensor_model", "TEXT")
        self._ensure_column("temperature_log", "thermocouple_type", "TEXT")
        self._ensure_column("temperature_log", "previous_good_temp_c", "REAL")
        self._ensure_column("temperature_log", "previous_good_temp_f", "REAL")
        self._ensure_column("temperature_log", "delta_from_previous_good_c", "REAL")
        self._ensure_column("temperature_log", "delta_from_previous_good_f", "REAL")
        self._ensure_column("temperature_log", "error_streak", "INTEGER")
        self._ensure_column("temperature_log", "seconds_since_last_good", "REAL")
        self._ensure_column("temperature_log", "last_good_timestamp_utc", "TEXT")
        self._ensure_column("temperature_log", "raw_frame_hex", "TEXT")
        self._ensure_column("temperature_log", "fault_bits_hex", "TEXT")
        self._ensure_column("temperature_log", "fault_flags", "TEXT")
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
                rule_name TEXT,
                snapshot_filename TEXT
            )
            """
        )
        self._ensure_column("alert_log", "rule_id", "INTEGER")
        self._ensure_column("alert_log", "rule_name", "TEXT")
        self._ensure_column("alert_log", "snapshot_filename", "TEXT")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_delivery_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                alert_timestamp_utc TEXT NOT NULL,
                rule_id INTEGER,
                rule_name TEXT,
                alert_kind TEXT,
                channel TEXT NOT NULL,
                success INTEGER NOT NULL,
                detail TEXT NOT NULL
            )
            """
        )
        self._ensure_column("alert_delivery_log", "alert_kind", "TEXT")
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_alert_log_timestamp_utc
            ON alert_log(timestamp_utc)
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_alert_delivery_log_timestamp_utc
            ON alert_delivery_log(timestamp_utc)
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
                trigger_minutes REAL,
                severity TEXT NOT NULL,
                hysteresis_f REAL NOT NULL DEFAULT 5.0,
                notify_cooldown_minutes REAL NOT NULL DEFAULT 15.0,
                color_hex TEXT NOT NULL DEFAULT '#38bdf8',
                notify_email INTEGER NOT NULL DEFAULT 0,
                notify_sms INTEGER NOT NULL DEFAULT 0,
                notify_push INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 0,
                last_triggered_at TEXT,
                last_triggered_context TEXT
            )
            """
        )
        self._ensure_column("alert_rules", "trigger_minutes", "REAL")
        self._ensure_column("alert_rules", "color_hex", "TEXT NOT NULL DEFAULT '#38bdf8'")
        self._ensure_column("alert_rules", "notify_cooldown_minutes", "REAL NOT NULL DEFAULT 15.0")
        self._ensure_column("alert_rules", "notify_email", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("alert_rules", "notify_sms", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("alert_rules", "notify_push", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("alert_rules", "last_triggered_context", "TEXT")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS kiln_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                event_type TEXT NOT NULL,
                label TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_kiln_events_timestamp_utc
            ON kiln_events(timestamp_utc)
            """
        )
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
                detail,
                cold_junction_c,
                sensor_model,
                thermocouple_type,
                previous_good_temp_c,
                previous_good_temp_f,
                delta_from_previous_good_c,
                delta_from_previous_good_f,
                error_streak,
                seconds_since_last_good,
                last_good_timestamp_utc,
                raw_frame_hex,
                fault_bits_hex,
                fault_flags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sample.timestamp.isoformat(),
                sample.temp_c,
                sample.temp_f,
                sample.status,
                sample.detail,
                sample.cold_junction_c,
                sample.sensor_model,
                sample.thermocouple_type,
                sample.previous_good_temp_c,
                sample.previous_good_temp_f,
                sample.delta_from_previous_good_c,
                sample.delta_from_previous_good_f,
                sample.error_streak,
                sample.seconds_since_last_good,
                sample.last_good_timestamp_utc,
                sample.raw_frame_hex,
                sample.fault_bits_hex,
                sample.fault_flags,
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
                rule_name,
                snapshot_filename
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                alert.snapshot_filename,
            ),
        )
        self._connection.commit()

    def fetch_alert_rules(self) -> list[AlertRule]:
        rows = self._connection.execute(
            """
            SELECT
                id,
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
                trigger_minutes=row[5],
                severity=row[6],
                hysteresis_f=row[7],
                notify_cooldown_minutes=row[8],
                color_hex=row[9],
                notify_email=bool(row[10]),
                notify_sms=bool(row[11]),
                notify_push=bool(row[12]),
                active=bool(row[13]),
                last_triggered_at=row[14],
                last_triggered_context=row[15],
            )
            for row in rows
        ]

    def update_alert_rule_state(self, rule: AlertRule) -> None:
        self._connection.execute(
            """
            UPDATE alert_rules
            SET active = ?, last_triggered_at = ?, last_triggered_context = ?
            WHERE id = ?
            """,
            (
                int(rule.active),
                rule.last_triggered_at,
                rule.last_triggered_context,
                rule.id,
            ),
        )
        self._connection.commit()

    def log_event(
        self,
        *,
        timestamp_utc: str,
        event_type: str,
        label: str,
        detail: str = "",
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO kiln_events (
                timestamp_utc,
                event_type,
                label,
                detail
            ) VALUES (?, ?, ?, ?)
            """,
            (timestamp_utc, event_type, label, detail),
        )
        self._connection.commit()

    def log_alert_delivery(
        self,
        alert: AlertEvent,
        channel: str,
        success: bool,
        detail: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO alert_delivery_log (
                timestamp_utc,
                alert_timestamp_utc,
                rule_id,
                rule_name,
                alert_kind,
                channel,
                success,
                detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                alert.timestamp_utc,
                alert.timestamp_utc,
                alert.rule_id,
                alert.rule_name,
                alert.kind,
                channel,
                int(success),
                detail,
            ),
        )
        self._connection.commit()

    def should_rate_limit_alert(
        self,
        alert: AlertEvent,
        channel: str,
        cooldown_minutes: float,
    ) -> bool:
        if cooldown_minutes <= 0 or alert.rule_id is None:
            return False

        row = self._connection.execute(
            """
            SELECT timestamp_utc
            FROM alert_delivery_log
            WHERE rule_id = ?
              AND channel = ?
              AND alert_kind = ?
              AND success = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (alert.rule_id, channel, alert.kind),
        ).fetchone()
        if row is None or row[0] is None:
            return False

        try:
            last_sent_at = datetime.fromisoformat(row[0])
        except ValueError:
            return False

        return (datetime.now(timezone.utc) - last_sent_at) < timedelta(minutes=cooldown_minutes)

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
