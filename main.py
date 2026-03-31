from __future__ import annotations

import argparse
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


def run_diagnostic() -> int:
    from config import MAX31856_CS_PIN, THERMOCOUPLE_TYPE

    print("Kiln Monitor Diagnostic")
    print(f"CS pin: {MAX31856_CS_PIN}")
    print(f"Thermocouple type: {THERMOCOUPLE_TYPE}")

    try:
        sensor = Max31856Reader()
    except Exception as exc:
        print("SPI/Sensor init: FAILED")
        print(f"Detail: {exc}")
        return 1

    try:
        sample = sensor.read_sample()
    except SensorReadError as exc:
        print("SPI/Sensor init: OK")
        print("Sensor read: FAULT")
        print(f"Faults: {exc}")
        return 2
    except Exception as exc:
        print("SPI/Sensor init: OK")
        print("Sensor read: FAILED")
        print(f"Detail: {exc}")
        return 1

    print("SPI/Sensor init: OK")
    print("Sensor read: OK")
    print(f"Temperature: {sample.temp_c:.2f} C / {sample.temp_f:.2f} F")
    print("Faults: none")
    return 0


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kiln temperature monitor")
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="Run a one-shot hardware check and exit.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.diagnostic:
        sys.exit(run_diagnostic())
    sys.exit(run())
