from __future__ import annotations

import csv
import gzip
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import (
    ARCHIVE_DIR,
    DATABASE_PATH,
    RETENTION_ARCHIVE_DAYS,
    RETENTION_SQLITE_DAYS,
)


ARCHIVE_PREFIX = "kiln_samples"
ARCHIVE_SUFFIX = ".csv.gz"
ARCHIVE_BATCH_SIZE = 5000


def format_sample_age(timestamp_utc: str) -> str:
    sample_time = datetime.fromisoformat(timestamp_utc)
    age_seconds = (datetime.now(timezone.utc) - sample_time).total_seconds()
    if age_seconds < 0:
        return "0s"
    if age_seconds < 86400:
        return f"{int(age_seconds // 3600)}h {int((age_seconds % 3600) // 60)}m"
    return f"{int(age_seconds // 86400)}d {int((age_seconds % 86400) // 3600)}h"


def build_archive_path(first_row: sqlite3.Row, last_row: sqlite3.Row) -> Path:
    first_timestamp = datetime.fromisoformat(first_row["timestamp_utc"]).strftime("%Y%m%dT%H%M%SZ")
    last_timestamp = datetime.fromisoformat(last_row["timestamp_utc"]).strftime("%Y%m%dT%H%M%SZ")
    archive_name = (
        f"{ARCHIVE_PREFIX}_{first_timestamp}_to_{last_timestamp}_"
        f"id{first_row['id']}-id{last_row['id']}{ARCHIVE_SUFFIX}"
    )
    return ARCHIVE_DIR / archive_name


def fetch_archive_bounds(
    connection: sqlite3.Connection,
    cutoff_utc: datetime,
) -> tuple[sqlite3.Row, sqlite3.Row] | tuple[None, None]:
    first_row = connection.execute(
        """
        SELECT id, timestamp_utc, temp_c, temp_f, status, detail
        FROM temperature_log
        WHERE timestamp_utc < ?
        ORDER BY id ASC
        LIMIT 1
        """,
        (cutoff_utc.isoformat(),),
    ).fetchone()

    if first_row is None:
        return None, None

    last_row = connection.execute(
        """
        SELECT id, timestamp_utc, temp_c, temp_f, status, detail
        FROM temperature_log
        WHERE timestamp_utc < ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (cutoff_utc.isoformat(),),
    ).fetchone()

    return first_row, last_row


def write_archive(
    connection: sqlite3.Connection,
    archive_path: Path,
    cutoff_utc: datetime,
    max_row_id: int,
) -> int:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
    archived_rows = 0

    with gzip.open(temp_path, "wt", newline="", encoding="utf-8") as archive_file:
        writer = csv.writer(archive_file)
        writer.writerow(["id", "timestamp_utc", "temp_c", "temp_f", "status", "detail"])
        cursor = connection.execute(
            """
            SELECT id, timestamp_utc, temp_c, temp_f, status, detail
            FROM temperature_log
            WHERE timestamp_utc < ?
              AND id <= ?
            ORDER BY id ASC
            """,
            (cutoff_utc.isoformat(), max_row_id),
        )

        while True:
            rows = cursor.fetchmany(ARCHIVE_BATCH_SIZE)
            if not rows:
                break

            for row in rows:
                writer.writerow(
                    [
                        row["id"],
                        row["timestamp_utc"],
                        row["temp_c"],
                        row["temp_f"],
                        row["status"],
                        row["detail"],
                    ]
                )
                archived_rows += 1

    temp_path.replace(archive_path)
    return archived_rows


def delete_archived_rows(
    connection: sqlite3.Connection,
    cutoff_utc: datetime,
    max_row_id: int,
) -> int:
    cursor = connection.execute(
        """
        DELETE FROM temperature_log
        WHERE timestamp_utc < ?
          AND id <= ?
        """,
        (cutoff_utc.isoformat(), max_row_id),
    )
    connection.commit()
    return cursor.rowcount


def prune_old_archives(now_utc: datetime) -> int:
    if not ARCHIVE_DIR.exists():
        return 0

    cutoff_utc = now_utc - timedelta(days=RETENTION_ARCHIVE_DAYS)
    removed_count = 0

    for archive_path in ARCHIVE_DIR.glob(f"{ARCHIVE_PREFIX}_*{ARCHIVE_SUFFIX}"):
        modified_at = datetime.fromtimestamp(archive_path.stat().st_mtime, timezone.utc)
        if modified_at < cutoff_utc:
            archive_path.unlink()
            removed_count += 1

    return removed_count


def run_retention() -> int:
    if RETENTION_SQLITE_DAYS < 30:
        print(
            "Refusing to run retention with "
            f"KILN_MONITOR_RETENTION_SQLITE_DAYS={RETENTION_SQLITE_DAYS}. "
            "Use 30 or more days."
        )
        return 1

    if RETENTION_ARCHIVE_DAYS < RETENTION_SQLITE_DAYS:
        print(
            "Refusing to run retention because archive retention is shorter than "
            f"SQLite retention: {RETENTION_ARCHIVE_DAYS} < {RETENTION_SQLITE_DAYS}"
        )
        return 1

    if not DATABASE_PATH.exists():
        print(f"Database not found: {DATABASE_PATH}")
        return 0

    now_utc = datetime.now(timezone.utc)
    cutoff_utc = now_utc - timedelta(days=RETENTION_SQLITE_DAYS)

    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute("PRAGMA busy_timeout=5000;")

    try:
        first_row, last_row = fetch_archive_bounds(connection, cutoff_utc)
        if first_row is None or last_row is None:
            print(
                "No rows eligible for archive. "
                f"SQLite cutoff: {cutoff_utc.isoformat()} "
                f"({RETENTION_SQLITE_DAYS} days)"
            )
            removed_archives = prune_old_archives(now_utc)
            print(f"Pruned archive files: {removed_archives}")
            return 0

        archive_path = build_archive_path(first_row, last_row)
        archived_rows = write_archive(
            connection,
            archive_path,
            cutoff_utc,
            last_row["id"],
        )
        deleted_rows = delete_archived_rows(connection, cutoff_utc, last_row["id"])
        removed_archives = prune_old_archives(now_utc)
    except sqlite3.Error as exc:
        print(f"Retention failed: {exc}")
        return 1
    finally:
        connection.close()

    print("Kiln retention complete")
    print(f"SQLite cutoff UTC: {cutoff_utc.isoformat()}")
    print(
        "Archived rows: "
        f"{archived_rows} "
        f"(id {first_row['id']} to {last_row['id']}, "
        f"age {format_sample_age(first_row['timestamp_utc'])} to "
        f"{format_sample_age(last_row['timestamp_utc'])})"
    )
    print(f"Archive file: {archive_path}")
    print(f"Deleted SQLite rows: {deleted_rows}")
    print(f"Pruned archive files: {removed_archives}")
    return 0


if __name__ == "__main__":
    sys.exit(run_retention())
