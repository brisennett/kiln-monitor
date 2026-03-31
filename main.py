from __future__ import annotations

import signal
import sys
import time

from config import (
    APP_LOG_PATH,
    DATABASE_PATH,
    ERROR_STREAK_WARNING_THRESHOLD,
    READ_INTERVAL_SECONDS,
    STATUS_EVERY_N_SAMPLES,
)
from sensor.max31856_reader import Max31856Reader, SensorReadError, TemperatureSample
from storage.sqlite_logger import SQLiteLogger
from utils.runtime import format_trend, setup_app_logger


def build_error_sample(detail: str) -> TemperatureSample:
    from datetime import datetime, timezone

    return TemperatureSample(
        timestamp=datetime.now(timezone.utc),
        temp_c=None,
        temp_f=None,
        status="ERROR",
        detail=detail,
    )


def persist_sample(storage: SQLiteLogger, sample: TemperatureSample, logger) -> None:
    try:
        storage.log_sample(sample)
    except Exception:
        logger.exception("failed to persist sample with status=%s", sample.status)


def run() -> int:
    logger = setup_app_logger(APP_LOG_PATH)
    storage = SQLiteLogger(DATABASE_PATH)
    sensor = Max31856Reader()

    should_stop = False

    def handle_stop(signum, _frame) -> None:
        nonlocal should_stop
        should_stop = True
        logger.info("shutdown requested by signal %s", signum)

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    logger.info("kiln monitor started")
    previous_temp_f = None
    success_count = 0
    error_streak = 0

    try:
        while not should_stop:
            loop_started = time.monotonic()

            try:
                sample = sensor.read_sample()
                persist_sample(storage, sample, logger)
                error_streak = 0
                success_count += 1

                if success_count % STATUS_EVERY_N_SAMPLES == 0:
                    trend = format_trend(sample.temp_f, previous_temp_f)
                    print(
                        f"{sample.timestamp.isoformat()} | "
                        f"{sample.temp_c:7.2f} C | {sample.temp_f:7.2f} F | {trend}"
                    )
                previous_temp_f = sample.temp_f
            except SensorReadError as exc:
                error_streak += 1
                error_sample = build_error_sample(str(exc))
                persist_sample(storage, error_sample, logger)

                if error_streak >= ERROR_STREAK_WARNING_THRESHOLD:
                    logger.warning("sensor read error (%s consecutive): %s", error_streak, exc)
                else:
                    logger.info("sensor read error: %s", exc)

                print(f"{error_sample.timestamp.isoformat()} | ERROR | {exc}")
            except Exception:
                logger.exception("unexpected runtime failure")

            elapsed = time.monotonic() - loop_started
            sleep_for = max(0.0, READ_INTERVAL_SECONDS - elapsed)
            time.sleep(sleep_for)
    finally:
        storage.close()
        logger.info("kiln monitor stopped")

    return 0


if __name__ == "__main__":
    sys.exit(run())
