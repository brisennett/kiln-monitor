from __future__ import annotations

import base64
import json
import logging
import smtplib
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Protocol
from urllib import error, parse, request
from urllib.parse import urlparse

from alerts import AlertEvent, AlertRule
from config import (
    ALERT_EMAIL_ENABLED,
    ALERT_EMAIL_FROM,
    ALERT_EMAIL_SMTP_HOST,
    ALERT_EMAIL_SMTP_PASSWORD,
    ALERT_EMAIL_SMTP_PORT,
    ALERT_EMAIL_SMTP_STARTTLS,
    ALERT_EMAIL_SMTP_USERNAME,
    ALERT_EMAIL_TO,
    ALERT_SLACK_ENABLED,
    ALERT_SLACK_WEBHOOK_URL,
    ALERT_SMS_ENABLED,
    ALERT_SMS_TO,
    ALERT_TWILIO_ACCOUNT_SID,
    ALERT_TWILIO_AUTH_TOKEN,
    ALERT_TWILIO_FROM,
    DATABASE_PATH,
)

LOGGER = logging.getLogger(__name__)


class NotificationError(Exception):
    """Raised when a notification channel cannot send an alert."""


@dataclass
class DeliveryResult:
    channel: str
    success: bool
    detail: str


class Notifier(Protocol):
    channel_name: str

    def is_enabled(self) -> bool:
        ...

    def send(self, alert: AlertEvent, rule: AlertRule) -> DeliveryResult:
        ...


def _merge_dict(base: dict, overrides: dict) -> dict:
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = _merge_dict(base[key], value)
        else:
            merged[key] = value
    return merged


def default_alert_channel_settings() -> dict:
    return {
        "email": {
            "enabled": ALERT_EMAIL_ENABLED,
            "smtp_host": ALERT_EMAIL_SMTP_HOST,
            "smtp_port": ALERT_EMAIL_SMTP_PORT,
            "starttls": ALERT_EMAIL_SMTP_STARTTLS,
            "username": ALERT_EMAIL_SMTP_USERNAME,
            "password": ALERT_EMAIL_SMTP_PASSWORD,
            "from_addr": ALERT_EMAIL_FROM,
            "to_addr": ALERT_EMAIL_TO,
        },
        "sms": {
            "enabled": ALERT_SMS_ENABLED,
            "account_sid": ALERT_TWILIO_ACCOUNT_SID,
            "auth_token": ALERT_TWILIO_AUTH_TOKEN,
            "from_number": ALERT_TWILIO_FROM,
            "to_number": ALERT_SMS_TO,
        },
        "slack": {
            "enabled": ALERT_SLACK_ENABLED,
            "webhook_url": ALERT_SLACK_WEBHOOK_URL,
        },
    }


def load_alert_channel_settings() -> dict:
    settings = default_alert_channel_settings()
    if not DATABASE_PATH.exists():
        return settings

    connection = sqlite3.connect(DATABASE_PATH)
    try:
        connection.row_factory = sqlite3.Row
        table_row = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'dashboard_state'
            """
        ).fetchone()
        if table_row is None:
            return settings

        row = connection.execute(
            "SELECT value FROM dashboard_state WHERE key = ?",
            ("alert_channel_settings",),
        ).fetchone()
        if row is None or not row["value"]:
            return settings

        stored = json.loads(row["value"])
        if not isinstance(stored, dict):
            return settings
        legacy_push = stored.pop("push", None)
        if "slack" not in stored and isinstance(legacy_push, dict):
            stored["slack"] = legacy_push
        return _merge_dict(settings, stored)
    except sqlite3.Error:
        LOGGER.warning("failed to load alert channel settings from sqlite", exc_info=True)
        return settings
    except json.JSONDecodeError:
        LOGGER.warning("failed to decode stored alert channel settings", exc_info=True)
        return settings
    finally:
        connection.close()


def build_enabled_notifiers() -> list[Notifier]:
    settings = load_alert_channel_settings()
    notifiers: list[Notifier] = [
        EmailNotifier(settings["email"]),
        TwilioSmsNotifier(settings["sms"]),
        SlackNotifier(settings["slack"]),
    ]
    return [notifier for notifier in notifiers if notifier.is_enabled()]


def channels_for_rule(rule: AlertRule) -> list[str]:
    channels: list[str] = []
    if rule.notify_email:
        channels.append("EMAIL")
    if rule.notify_sms:
        channels.append("SMS")
    if rule.notify_push:
        channels.append("PUSH")
    return channels


class EmailNotifier:
    channel_name = "EMAIL"

    def __init__(self, settings: dict | None = None) -> None:
        self.settings = settings or load_alert_channel_settings()["email"]

    def is_enabled(self) -> bool:
        return (
            bool(self.settings.get("enabled"))
            and bool(self.settings.get("smtp_host"))
            and bool(self.settings.get("from_addr"))
            and bool(self.settings.get("to_addr"))
        )

    def send(self, alert: AlertEvent, rule: AlertRule) -> DeliveryResult:
        if not self.is_enabled():
            raise NotificationError("email notifier is not configured")

        message = EmailMessage()
        message["Subject"] = f"[Kiln] {alert.level} {rule.name}"
        message["From"] = self.settings["from_addr"]
        message["To"] = self.settings["to_addr"]
        message.set_content(
            "\n".join(
                [
                    f"Level: {alert.level}",
                    f"Rule: {rule.name}",
                    f"Kind: {alert.kind}",
                    f"Time UTC: {alert.timestamp_utc}",
                    f"Detail: {alert.detail}",
                ]
            )
        )

        try:
            with smtplib.SMTP(self.settings["smtp_host"], int(self.settings["smtp_port"]), timeout=10) as smtp:
                if self.settings.get("starttls"):
                    smtp.starttls()
                if self.settings.get("username"):
                    smtp.login(self.settings["username"], self.settings.get("password", ""))
                smtp.send_message(message)
        except Exception as exc:
            raise NotificationError(f"email delivery failed: {exc}") from exc

        return DeliveryResult(channel=self.channel_name, success=True, detail=f"sent to {self.settings['to_addr']}")


class TwilioSmsNotifier:
    channel_name = "SMS"

    def __init__(self, settings: dict | None = None) -> None:
        self.settings = settings or load_alert_channel_settings()["sms"]

    def is_enabled(self) -> bool:
        return (
            bool(self.settings.get("enabled"))
            and bool(self.settings.get("account_sid"))
            and bool(self.settings.get("auth_token"))
            and bool(self.settings.get("from_number"))
            and bool(self.settings.get("to_number"))
        )

    def send(self, alert: AlertEvent, rule: AlertRule) -> DeliveryResult:
        if not self.is_enabled():
            raise NotificationError("twilio sms notifier is not configured")

        body_text = f"[{alert.level}] {rule.name}: {alert.detail}"
        endpoint = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{self.settings['account_sid']}/Messages.json"
        )
        form_body = parse.urlencode(
            {
                "From": self.settings["from_number"],
                "To": self.settings["to_number"],
                "Body": body_text,
            }
        ).encode("utf-8")
        auth_token = base64.b64encode(
            f"{self.settings['account_sid']}:{self.settings['auth_token']}".encode("utf-8")
        ).decode("ascii")
        request_obj = request.Request(
            endpoint,
            data=form_body,
            headers={
                "Authorization": f"Basic {auth_token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )

        try:
            with request.urlopen(request_obj, timeout=10) as response:
                if response.status >= 300:
                    raise NotificationError(f"twilio sms returned HTTP {response.status}")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise NotificationError(f"twilio sms failed: HTTP {exc.code} {detail}") from exc
        except Exception as exc:
            raise NotificationError(f"twilio sms failed: {exc}") from exc

        return DeliveryResult(channel=self.channel_name, success=True, detail=f"sent to {self.settings['to_number']}")


def _is_valid_slack_webhook_url(value: str) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    if parsed.scheme != "https":
        return False
    if parsed.netloc not in {"hooks.slack.com", "hooks.slack-gov.com"}:
        return False
    return parsed.path.startswith("/services/")


def _format_temperature_line(alert: AlertEvent) -> str:
    if alert.temp_f is None or alert.temp_c is None:
        return "unavailable"
    return f"{alert.temp_f:.2f} F / {alert.temp_c:.2f} C"


def _format_local_timestamp(timestamp_utc: str) -> str:
    try:
        parsed = datetime.fromisoformat(timestamp_utc.replace("Z", "+00:00"))
    except ValueError:
        return timestamp_utc
    if parsed.tzinfo is None:
        return timestamp_utc
    local_time = parsed.astimezone()
    timezone_label = local_time.tzname() or "local"
    return f"{local_time.strftime('%Y-%m-%d %I:%M:%S %p')} {timezone_label}"


def _build_slack_payload(alert: AlertEvent, rule: AlertRule) -> dict:
    summary = f"[{alert.level}] {rule.name}: {alert.detail}"
    fields = [
        {
            "type": "mrkdwn",
            "text": f"*Level*\n{alert.level}",
        },
        {
            "type": "mrkdwn",
            "text": f"*Rule*\n{rule.name}",
        },
        {
            "type": "mrkdwn",
            "text": f"*Kind*\n{alert.kind}",
        },
        {
            "type": "mrkdwn",
            "text": f"*Time Local*\n{_format_local_timestamp(alert.timestamp_utc)}",
        },
        {
            "type": "mrkdwn",
            "text": f"*Temperature*\n{_format_temperature_line(alert)}",
        },
    ]
    if alert.snapshot_filename:
        fields.append(
            {
                "type": "mrkdwn",
                "text": f"*Snapshot*\n{alert.snapshot_filename}",
            }
        )

    return {
        "text": summary,
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"Kiln alert: {rule.name}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{alert.level}* alert for `{alert.kind}`",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": alert.detail,
                },
            },
            {
                "type": "section",
                "fields": fields,
            },
        ],
    }


class SlackNotifier:
    channel_name = "PUSH"

    def __init__(self, settings: dict | None = None) -> None:
        self.settings = settings or load_alert_channel_settings()["slack"]

    def is_enabled(self) -> bool:
        return bool(self.settings.get("enabled")) and _is_valid_slack_webhook_url(
            str(self.settings.get("webhook_url", "")).strip()
        )

    def send(self, alert: AlertEvent, rule: AlertRule) -> DeliveryResult:
        if not self.is_enabled():
            raise NotificationError(
                "slack notifier is not configured with a valid incoming webhook URL"
            )

        payload = json.dumps(_build_slack_payload(alert, rule)).encode("utf-8")
        request_obj = request.Request(
            self.settings["webhook_url"],
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(request_obj, timeout=10) as response:
                if response.status >= 300:
                    raise NotificationError(f"slack webhook returned HTTP {response.status}")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise NotificationError(f"slack webhook failed: HTTP {exc.code} {detail}") from exc
        except Exception as exc:
            raise NotificationError(f"slack webhook failed: {exc}") from exc

        return DeliveryResult(
            channel=self.channel_name,
            success=True,
            detail="slack webhook accepted",
        )
