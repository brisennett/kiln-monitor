from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sensor.common import TemperatureSample


RULE_TYPES = {
    "TARGET_REACHED",
    "ABOVE_HIGH",
    "BELOW_LOW",
}

SEVERITY_ORDER = {
    "INFO": 0,
    "WARNING": 1,
    "CRITICAL": 2,
}


@dataclass
class AlertEvent:
    timestamp_utc: str
    level: str
    kind: str
    detail: str
    temp_c: float | None
    temp_f: float | None
    rule_id: int | None = None
    rule_name: str | None = None


@dataclass
class AlertRule:
    id: int | None
    name: str
    enabled: bool
    rule_type: str
    threshold_f: float
    severity: str
    hysteresis_f: float
    active: bool = False
    last_triggered_at: str | None = None


def validate_rule(rule: AlertRule) -> None:
    if not rule.name.strip():
        raise ValueError("alert rule name is required")
    if rule.rule_type not in RULE_TYPES:
        raise ValueError(f"unsupported alert rule type: {rule.rule_type}")
    if rule.severity not in SEVERITY_ORDER:
        raise ValueError(f"unsupported alert severity: {rule.severity}")
    if rule.hysteresis_f < 0:
        raise ValueError("alert hysteresis must be zero or greater")


def evaluate_alert_rules(
    sample: TemperatureSample,
    rules: list[AlertRule],
) -> tuple[list[AlertEvent], list[AlertRule]]:
    if sample.status != "OK" or sample.temp_f is None or sample.temp_c is None:
        return [], rules

    events: list[AlertEvent] = []
    updated_rules: list[AlertRule] = []

    for rule in rules:
        validate_rule(rule)
        if not rule.enabled:
            updated_rules.append(replace(rule, active=False))
            continue

        if rule.rule_type == "TARGET_REACHED":
            next_rule, next_events = evaluate_target_rule(sample, rule)
        elif rule.rule_type == "ABOVE_HIGH":
            next_rule, next_events = evaluate_above_high_rule(sample, rule)
        else:
            next_rule, next_events = evaluate_below_low_rule(sample, rule)

        updated_rules.append(next_rule)
        events.extend(next_events)

    return events, updated_rules


def evaluate_target_rule(
    sample: TemperatureSample,
    rule: AlertRule,
) -> tuple[AlertRule, list[AlertEvent]]:
    if rule.active:
        if sample.temp_f <= (rule.threshold_f - rule.hysteresis_f):
            return replace(rule, active=False), []
        return rule, []

    if sample.temp_f >= rule.threshold_f:
        fired_rule = replace(rule, active=True, last_triggered_at=sample.timestamp.isoformat())
        return fired_rule, [
            AlertEvent(
                timestamp_utc=sample.timestamp.isoformat(),
                level=rule.severity,
                kind="TARGET_REACHED",
                detail=(
                    f"{rule.name} reached at {sample.temp_f:.2f} F / "
                    f"{sample.temp_c:.2f} C"
                ),
                temp_c=sample.temp_c,
                temp_f=sample.temp_f,
                rule_id=rule.id,
                rule_name=rule.name,
            )
        ]

    return rule, []


def evaluate_above_high_rule(
    sample: TemperatureSample,
    rule: AlertRule,
) -> tuple[AlertRule, list[AlertEvent]]:
    if not rule.active and sample.temp_f >= rule.threshold_f:
        fired_rule = replace(rule, active=True, last_triggered_at=sample.timestamp.isoformat())
        return fired_rule, [
            AlertEvent(
                timestamp_utc=sample.timestamp.isoformat(),
                level=rule.severity,
                kind="ABOVE_HIGH_TRIGGER",
                detail=(
                    f"{rule.name} triggered high at {sample.temp_f:.2f} F / "
                    f"{sample.temp_c:.2f} C"
                ),
                temp_c=sample.temp_c,
                temp_f=sample.temp_f,
                rule_id=rule.id,
                rule_name=rule.name,
            )
        ]

    if rule.active and sample.temp_f <= (rule.threshold_f - rule.hysteresis_f):
        cleared_rule = replace(rule, active=False)
        return cleared_rule, [
            AlertEvent(
                timestamp_utc=sample.timestamp.isoformat(),
                level="INFO",
                kind="ABOVE_HIGH_CLEAR",
                detail=(
                    f"{rule.name} cleared high at {sample.temp_f:.2f} F / "
                    f"{sample.temp_c:.2f} C"
                ),
                temp_c=sample.temp_c,
                temp_f=sample.temp_f,
                rule_id=rule.id,
                rule_name=rule.name,
            )
        ]

    return rule, []


def evaluate_below_low_rule(
    sample: TemperatureSample,
    rule: AlertRule,
) -> tuple[AlertRule, list[AlertEvent]]:
    if not rule.active and sample.temp_f <= rule.threshold_f:
        fired_rule = replace(rule, active=True, last_triggered_at=sample.timestamp.isoformat())
        return fired_rule, [
            AlertEvent(
                timestamp_utc=sample.timestamp.isoformat(),
                level=rule.severity,
                kind="BELOW_LOW_TRIGGER",
                detail=(
                    f"{rule.name} triggered low at {sample.temp_f:.2f} F / "
                    f"{sample.temp_c:.2f} C"
                ),
                temp_c=sample.temp_c,
                temp_f=sample.temp_f,
                rule_id=rule.id,
                rule_name=rule.name,
            )
        ]

    if rule.active and sample.temp_f >= (rule.threshold_f + rule.hysteresis_f):
        cleared_rule = replace(rule, active=False)
        return cleared_rule, [
            AlertEvent(
                timestamp_utc=sample.timestamp.isoformat(),
                level="INFO",
                kind="BELOW_LOW_CLEAR",
                detail=(
                    f"{rule.name} cleared low at {sample.temp_f:.2f} F / "
                    f"{sample.temp_c:.2f} C"
                ),
                temp_c=sample.temp_c,
                temp_f=sample.temp_f,
                rule_id=rule.id,
                rule_name=rule.name,
            )
        ]

    return rule, []
