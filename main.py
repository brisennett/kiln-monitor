from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, timezone

from alerts import AlertEvent, AlertRule, evaluate_alert_rules
from config import (
    APP_LOG_PATH,
    DATABASE_PATH,
    ERROR_STREAK_WARNING_THRESHOLD,
    MAX_SAMPLE_JUMP_C,
    SENSOR_MODEL,
    READ_INTERVAL_SECONDS,
    SPI_CS_PIN,
    STATUS_EVERY_N_SAMPLES,
    THERMOCOUPLE_TYPE,
    WATCHDOG_FAULT_STREAK_THRESHOLD,
    WATCHDOG_NOTIFY_COOLDOWN_MINUTES,
    WATCHDOG_STALE_DATA_SECONDS,
)
from notifiers import NotificationError, build_enabled_notifiers, channels_for_rule
from sensor import SensorReadError, TemperatureSample, build_sensor_reader
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


def persist_alerts(storage: SQLiteLogger, alerts, logger) -> None:
    for alert in alerts:
        try:
            storage.log_alert(alert)
        except Exception:
            logger.exception("failed to persist alert with level=%s kind=%s", alert.level, alert.kind)


def build_watchdog_rule(rule_id: int, name: str) -> AlertRule:
    return AlertRule(
        id=rule_id,
        name=name,
        enabled=True,
        rule_type="TARGET_REACHED",
        threshold_f=0.0,
        severity="CRITICAL",
        hysteresis_f=0.0,
        notify_cooldown_minutes=WATCHDOG_NOTIFY_COOLDOWN_MINUTES,
        color_hex="#ef4444",
        notify_email=True,
        notify_sms=True,
        notify_push=True,
        active=False,
        last_triggered_at=None,
    )


def deliver_alerts(storage: SQLiteLogger, alerts, updated_rules, logger) -> None:
    rule_by_id = {
        rule.id: rule
        for rule in updated_rules
        if rule.id is not None
    }
    notifiers = {
        notifier.channel_name: notifier
        for notifier in build_enabled_notifiers()
    }

    for alert in alerts:
        if alert.rule_id is None:
            continue

        rule = rule_by_id.get(alert.rule_id)
        if rule is None:
            continue

        for channel in channels_for_rule(rule):
            notifier = notifiers.get(channel)
            if notifier is None:
                try:
                    storage.log_alert_delivery(
                        alert,
                        channel=channel,
                        success=False,
                        detail="channel enabled on rule but notifier is not configured globally",
                    )
                except Exception:
                    logger.exception("failed to log alert delivery failure for %s", channel)
                continue

            if storage.should_rate_limit_alert(
                alert,
                channel=channel,
                cooldown_minutes=rule.notify_cooldown_minutes,
            ):
                detail = (
                    f"suppressed by cooldown ({rule.notify_cooldown_minutes:.1f} min)"
                )
                try:
                    storage.log_alert_delivery(
                        alert,
                        channel=channel,
                        success=False,
                        detail=detail,
                    )
                except Exception:
                    logger.exception("failed to log rate-limited alert delivery for %s", channel)
                logger.info("alert delivery skipped via %s: %s", channel, detail)
                continue

            try:
                result = notifier.send(alert, rule)
                storage.log_alert_delivery(
                    alert,
                    channel=result.channel,
                    success=result.success,
                    detail=result.detail,
                )
                logger.info("alert delivered via %s: %s", result.channel, result.detail)
            except NotificationError as exc:
                try:
                    storage.log_alert_delivery(
                        alert,
                        channel=channel,
                        success=False,
                        detail=str(exc),
                    )
                except Exception:
                    logger.exception("failed to log alert delivery failure for %s", channel)
                logger.warning("alert delivery failed via %s: %s", channel, exc)


def deliver_watchdog_alerts(storage: SQLiteLogger, alerts, logger) -> None:
    if not alerts:
        return

    notifiers = build_enabled_notifiers()
    for alert in alerts:
        if alert.rule_id is None or alert.rule_name is None:
            continue

        rule = build_watchdog_rule(alert.rule_id, alert.rule_name)
        for notifier in notifiers:
            channel = notifier.channel_name
            if storage.should_rate_limit_alert(
                alert,
                channel=channel,
                cooldown_minutes=rule.notify_cooldown_minutes,
            ):
                detail = (
                    f"suppressed by watchdog cooldown ({rule.notify_cooldown_minutes:.1f} min)"
                )
                try:
                    storage.log_alert_delivery(
                        alert,
                        channel=channel,
                        success=False,
                        detail=detail,
                    )
                except Exception:
                    logger.exception("failed to log watchdog rate-limited alert for %s", channel)
                logger.info("watchdog alert skipped via %s: %s", channel, detail)
                continue

            try:
                result = notifier.send(alert, rule)
                storage.log_alert_delivery(
                    alert,
                    channel=result.channel,
                    success=result.success,
                    detail=result.detail,
                )
                logger.warning("watchdog alert delivered via %s: %s", result.channel, result.detail)
            except NotificationError as exc:
                try:
                    storage.log_alert_delivery(
                        alert,
                        channel=channel,
                        success=False,
                        detail=str(exc),
                    )
                except Exception:
                    logger.exception("failed to log watchdog delivery failure for %s", channel)
                logger.warning("watchdog alert delivery failed via %s: %s", channel, exc)


def emit_watchdog_alerts(storage: SQLiteLogger, alerts, logger) -> None:
    if not alerts:
        return
    persist_alerts(storage, alerts, logger)
    deliver_watchdog_alerts(storage, alerts, logger)
    for alert in alerts:
        logger.warning("watchdog %s: %s", alert.kind, alert.detail)
        print(f"{alert.timestamp_utc} | WATCHDOG {alert.level} | {alert.detail}")


def reject_unrealistic_jump(sample: TemperatureSample, previous_temp_c: float | None) -> None:
    if previous_temp_c is None or sample.temp_c is None:
        return

    delta_c = sample.temp_c - previous_temp_c
    if abs(delta_c) > MAX_SAMPLE_JUMP_C:
        raise SensorReadError(
            f"unrealistic temperature jump: {delta_c:+.2f} C "
            f"(from {previous_temp_c:.2f} C to {sample.temp_c:.2f} C)"
        )


def run_diagnostic(sample_count: int, sample_delay_seconds: float) -> int:
    print("Kiln Monitor Diagnostic")
    print(f"Sensor model: {SENSOR_MODEL}")
    print(f"CS pin: {SPI_CS_PIN}")
    print(f"Thermocouple type: {THERMOCOUPLE_TYPE}")
    print(f"Samples: {sample_count}")
    print(f"Sample delay: {sample_delay_seconds:.2f} seconds")

    try:
        sensor = build_sensor_reader()
    except Exception as exc:
        print("SPI/Sensor init: FAILED")
        print(f"Detail: {exc}")
        return 1

    print("SPI/Sensor init: OK")

    previous_temp_c = None
    fault_count = 0

    for sample_number in range(1, sample_count + 1):
        try:
            sample = sensor.read_sample()
            reject_unrealistic_jump(sample, previous_temp_c)
            delta_text = "n/a"
            if previous_temp_c is not None:
                delta_c = sample.temp_c - previous_temp_c
                delta_text = f"{delta_c:+.2f} C"

            print(
                f"Sample {sample_number:02d}: "
                f"{sample.temp_c:7.2f} C / {sample.temp_f:7.2f} F | "
                f"delta {delta_text} | OK"
            )
            previous_temp_c = sample.temp_c
        except SensorReadError as exc:
            fault_count += 1
            print(f"Sample {sample_number:02d}: FAULT | {exc}")
        except Exception as exc:
            print(f"Sample {sample_number:02d}: FAILED | {exc}")
            return 1

        if sample_number < sample_count:
            time.sleep(sample_delay_seconds)

    if fault_count:
        print(f"Diagnostic complete: {fault_count} fault sample(s) out of {sample_count}")
        return 2

    print("Diagnostic complete: all samples OK")
    return 0


def run() -> int:
    logger = setup_app_logger(APP_LOG_PATH)
    storage = SQLiteLogger(DATABASE_PATH)
    sensor = build_sensor_reader()

    should_stop = False

    def handle_stop(signum, _frame) -> None:
        nonlocal should_stop
        should_stop = True
        logger.info("shutdown requested by signal %s", signum)

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    logger.info("kiln monitor started")
    previous_temp_c = None
    success_count = 0
    error_streak = 0
    last_good_sample_at = datetime.now(timezone.utc)
    watchdog_fault_active = False
    watchdog_stale_active = False

    try:
        while not should_stop:
            loop_started = time.monotonic()
            loop_completed_with_good_sample = False

            try:
                sample = sensor.read_sample()
                reject_unrealistic_jump(sample, previous_temp_c)
                persist_sample(storage, sample, logger)
                watchdog_alerts = []
                if watchdog_fault_active:
                    watchdog_alerts.append(
                        AlertEvent(
                            timestamp_utc=sample.timestamp.isoformat(),
                            level="INFO",
                            kind="WATCHDOG_FAULT_STREAK_CLEAR",
                            detail=f"fault streak cleared after {error_streak} consecutive read error(s)",
                            temp_c=sample.temp_c,
                            temp_f=sample.temp_f,
                            rule_id=-1,
                            rule_name="Watchdog Fault Streak",
                        )
                    )
                    watchdog_fault_active = False
                if watchdog_stale_active:
                    stale_age = (sample.timestamp - last_good_sample_at).total_seconds()
                    watchdog_alerts.append(
                        AlertEvent(
                            timestamp_utc=sample.timestamp.isoformat(),
                            level="INFO",
                            kind="WATCHDOG_STALE_DATA_CLEAR",
                            detail=f"stale data cleared after fresh sample arrived ({int(max(stale_age, 0))}s since last good sample)",
                            temp_c=sample.temp_c,
                            temp_f=sample.temp_f,
                            rule_id=-2,
                            rule_name="Watchdog Stale Data",
                        )
                    )
                    watchdog_stale_active = False
                emit_watchdog_alerts(storage, watchdog_alerts, logger)
                alert_rules = storage.fetch_alert_rules()
                alerts, updated_rules = evaluate_alert_rules(sample, alert_rules)
                persist_alerts(storage, alerts, logger)
                for original_rule, updated_rule in zip(alert_rules, updated_rules):
                    if (
                        updated_rule.id is not None
                        and (
                            original_rule.active != updated_rule.active
                            or original_rule.last_triggered_at != updated_rule.last_triggered_at
                        )
                    ):
                        storage.update_alert_rule_state(updated_rule)
                deliver_alerts(storage, alerts, updated_rules, logger)
                for alert in alerts:
                    if alert.level == "CRITICAL":
                        logger.warning("alert %s: %s", alert.kind, alert.detail)
                    else:
                        logger.info("alert %s: %s", alert.kind, alert.detail)
                    print(f"{alert.timestamp_utc} | ALERT {alert.level} | {alert.detail}")
                error_streak = 0
                success_count += 1
                last_good_sample_at = sample.timestamp
                loop_completed_with_good_sample = True

                if success_count % STATUS_EVERY_N_SAMPLES == 0:
                    previous_temp_f = None
                    if previous_temp_c is not None:
                        previous_temp_f = (previous_temp_c * 9.0 / 5.0) + 32.0
                    trend = format_trend(sample.temp_f, previous_temp_f)
                    print(
                        f"{sample.timestamp.isoformat()} | "
                        f"{sample.temp_c:7.2f} C | {sample.temp_f:7.2f} F | {trend}"
                    )
                previous_temp_c = sample.temp_c
            except SensorReadError as exc:
                error_streak += 1
                error_sample = build_error_sample(str(exc))
                persist_sample(storage, error_sample, logger)
                watchdog_alerts = []

                if error_streak >= WATCHDOG_FAULT_STREAK_THRESHOLD and not watchdog_fault_active:
                    watchdog_alerts.append(
                        AlertEvent(
                            timestamp_utc=error_sample.timestamp.isoformat(),
                            level="CRITICAL",
                            kind="WATCHDOG_FAULT_STREAK_TRIGGER",
                            detail=(
                                f"watchdog fault streak: {error_streak} consecutive sensor read "
                                f"errors; last error: {exc}"
                            ),
                            temp_c=None,
                            temp_f=None,
                            rule_id=-1,
                            rule_name="Watchdog Fault Streak",
                        )
                    )
                    watchdog_fault_active = True

                if error_streak >= ERROR_STREAK_WARNING_THRESHOLD:
                    logger.warning("sensor read error (%s consecutive): %s", error_streak, exc)
                else:
                    logger.info("sensor read error: %s", exc)

                emit_watchdog_alerts(storage, watchdog_alerts, logger)
                print(f"{error_sample.timestamp.isoformat()} | ERROR | {exc}")
            except Exception:
                logger.exception("unexpected runtime failure")

            if not loop_completed_with_good_sample:
                stale_age_seconds = (datetime.now(timezone.utc) - last_good_sample_at).total_seconds()
                if stale_age_seconds >= WATCHDOG_STALE_DATA_SECONDS and not watchdog_stale_active:
                    watchdog_stale_active = True
                    emit_watchdog_alerts(
                        storage,
                        [
                            AlertEvent(
                                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                                level="CRITICAL",
                                kind="WATCHDOG_STALE_DATA_TRIGGER",
                                detail=(
                                    f"watchdog stale data: no successful temperature sample "
                                    f"for {int(stale_age_seconds)} seconds"
                                ),
                                temp_c=None,
                                temp_f=None,
                                rule_id=-2,
                                rule_name="Watchdog Stale Data",
                            )
                        ],
                        logger,
                    )

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
    parser.add_argument(
        "--diagnostic-samples",
        type=int,
        default=10,
        help="Number of samples to read in diagnostic mode.",
    )
    parser.add_argument(
        "--diagnostic-delay-seconds",
        type=float,
        default=1.0,
        help="Delay between diagnostic samples.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.diagnostic:
        sys.exit(run_diagnostic(args.diagnostic_samples, args.diagnostic_delay_seconds))
    sys.exit(run())
