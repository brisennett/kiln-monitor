from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone

from config import DATABASE_PATH


def format_sample_age(timestamp_utc: str) -> str:
    sample_time = datetime.fromisoformat(timestamp_utc)
    age_seconds = (datetime.now(timezone.utc) - sample_time).total_seconds()
    if age_seconds < 0:
        return "0s"
    if age_seconds < 60:
        return f"{int(age_seconds)}s"
    if age_seconds < 3600:
        return f"{int(age_seconds // 60)}m {int(age_seconds % 60)}s"
    return f"{int(age_seconds // 3600)}h {int((age_seconds % 3600) // 60)}m"


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def print_status() -> int:
    if not DATABASE_PATH.exists():
        print(f"Database not found: {DATABASE_PATH}")
        return 1

    connection = sqlite3.connect(DATABASE_PATH)
    try:
        latest_sample = connection.execute(
            """
            SELECT id, timestamp_utc, temp_c, temp_f, status, detail
            FROM temperature_log
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        total_rows = connection.execute(
            "SELECT COUNT(*) FROM temperature_log"
        ).fetchone()[0]

        latest_fault = connection.execute(
            """
            SELECT id, timestamp_utc, detail
            FROM temperature_log
            WHERE status = 'ERROR'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        latest_alert = None
        if table_exists(connection, "alert_log"):
            latest_alert = connection.execute(
                """
                SELECT id, timestamp_utc, level, kind, detail
                FROM alert_log
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
    except sqlite3.Error as exc:
        print(f"Failed to read status database: {exc}")
        return 1
    finally:
        connection.close()

    print("Kiln Monitor Status")
    print(f"Database: {DATABASE_PATH}")
    print(f"Total samples: {total_rows}")

    if latest_sample is None:
        print("Latest sample: none")
        return 0

    sample_id, timestamp_utc, temp_c, temp_f, status, detail = latest_sample
    sample_age = format_sample_age(timestamp_utc)

    print(f"Latest sample id: {sample_id}")
    print(f"Latest timestamp UTC: {timestamp_utc}")
    print(f"Latest sample age: {sample_age}")
    print(f"Latest status: {status}")

    if temp_c is not None and temp_f is not None:
        print(f"Latest temperature: {temp_c:.2f} C / {temp_f:.2f} F")
    else:
        print("Latest temperature: n/a")

    if detail:
        print(f"Latest detail: {detail}")

    if latest_fault is None:
        print("Last fault: none recorded")
    else:
        fault_id, fault_timestamp_utc, fault_detail = latest_fault
        fault_age = format_sample_age(fault_timestamp_utc)
        print(
            "Last fault: "
            f"id={fault_id}, "
            f"time_utc={fault_timestamp_utc}, "
            f"age={fault_age}, "
            f"detail={fault_detail}"
        )

    if latest_alert is None:
        print("Last alert: none recorded")
    else:
        alert_id, alert_timestamp_utc, alert_level, alert_kind, alert_detail = latest_alert
        alert_age = format_sample_age(alert_timestamp_utc)
        print(
            "Last alert: "
            f"id={alert_id}, "
            f"time_utc={alert_timestamp_utc}, "
            f"age={alert_age}, "
            f"level={alert_level}, "
            f"kind={alert_kind}, "
            f"detail={alert_detail}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(print_status())
