from __future__ import annotations

import base64
import json
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol
from urllib import error, parse, request

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
    ALERT_PUSH_ENABLED,
    ALERT_PUSH_WEBHOOK_URL,
    ALERT_SMS_ENABLED,
    ALERT_SMS_TO,
    ALERT_TWILIO_ACCOUNT_SID,
    ALERT_TWILIO_AUTH_TOKEN,
    ALERT_TWILIO_FROM,
)


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


def build_enabled_notifiers() -> list[Notifier]:
    notifiers: list[Notifier] = [
        EmailNotifier(),
        TwilioSmsNotifier(),
        WebhookPushNotifier(),
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

    def is_enabled(self) -> bool:
        return (
            ALERT_EMAIL_ENABLED
            and bool(ALERT_EMAIL_SMTP_HOST)
            and bool(ALERT_EMAIL_FROM)
            and bool(ALERT_EMAIL_TO)
        )

    def send(self, alert: AlertEvent, rule: AlertRule) -> DeliveryResult:
        if not self.is_enabled():
            raise NotificationError("email notifier is not configured")

        message = EmailMessage()
        message["Subject"] = f"[Kiln] {alert.level} {rule.name}"
        message["From"] = ALERT_EMAIL_FROM
        message["To"] = ALERT_EMAIL_TO
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
            with smtplib.SMTP(ALERT_EMAIL_SMTP_HOST, ALERT_EMAIL_SMTP_PORT, timeout=10) as smtp:
                if ALERT_EMAIL_SMTP_STARTTLS:
                    smtp.starttls()
                if ALERT_EMAIL_SMTP_USERNAME:
                    smtp.login(ALERT_EMAIL_SMTP_USERNAME, ALERT_EMAIL_SMTP_PASSWORD)
                smtp.send_message(message)
        except Exception as exc:
            raise NotificationError(f"email delivery failed: {exc}") from exc

        return DeliveryResult(channel=self.channel_name, success=True, detail=f"sent to {ALERT_EMAIL_TO}")


class TwilioSmsNotifier:
    channel_name = "SMS"

    def is_enabled(self) -> bool:
        return (
            ALERT_SMS_ENABLED
            and bool(ALERT_TWILIO_ACCOUNT_SID)
            and bool(ALERT_TWILIO_AUTH_TOKEN)
            and bool(ALERT_TWILIO_FROM)
            and bool(ALERT_SMS_TO)
        )

    def send(self, alert: AlertEvent, rule: AlertRule) -> DeliveryResult:
        if not self.is_enabled():
            raise NotificationError("twilio sms notifier is not configured")

        body_text = f"[{alert.level}] {rule.name}: {alert.detail}"
        endpoint = (
            f"https://api.twilio.com/2010-04-01/Accounts/"
            f"{ALERT_TWILIO_ACCOUNT_SID}/Messages.json"
        )
        form_body = parse.urlencode(
            {
                "From": ALERT_TWILIO_FROM,
                "To": ALERT_SMS_TO,
                "Body": body_text,
            }
        ).encode("utf-8")
        auth_token = base64.b64encode(
            f"{ALERT_TWILIO_ACCOUNT_SID}:{ALERT_TWILIO_AUTH_TOKEN}".encode("utf-8")
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

        return DeliveryResult(channel=self.channel_name, success=True, detail=f"sent to {ALERT_SMS_TO}")


class WebhookPushNotifier:
    channel_name = "PUSH"

    def is_enabled(self) -> bool:
        return ALERT_PUSH_ENABLED and bool(ALERT_PUSH_WEBHOOK_URL)

    def send(self, alert: AlertEvent, rule: AlertRule) -> DeliveryResult:
        if not self.is_enabled():
            raise NotificationError("push notifier is not configured")

        payload = json.dumps(
            {
                "level": alert.level,
                "rule_name": rule.name,
                "kind": alert.kind,
                "timestamp_utc": alert.timestamp_utc,
                "detail": alert.detail,
                "temp_c": alert.temp_c,
                "temp_f": alert.temp_f,
            }
        ).encode("utf-8")
        request_obj = request.Request(
            ALERT_PUSH_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with request.urlopen(request_obj, timeout=10) as response:
                if response.status >= 300:
                    raise NotificationError(f"push webhook returned HTTP {response.status}")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise NotificationError(f"push webhook failed: HTTP {exc.code} {detail}") from exc
        except Exception as exc:
            raise NotificationError(f"push webhook failed: {exc}") from exc

        return DeliveryResult(channel=self.channel_name, success=True, detail="push webhook accepted")
