"""Email delivery channel via SMTP (Gmail default).

Reads credentials from env: IIC_SMTP_USER, IIC_SMTP_APP_PASSWORD.
If smtp.enabled is False, the send is recorded as skipped (no retry).
"""

from __future__ import annotations

import os
import smtplib
import hashlib
import logging
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Dict

from tradingagents.delivery.base import DeliveryChannel, DeliveryError
from tradingagents.delivery.html_safety import sanitize_email_html
from tradingagents.persistence import store


log = logging.getLogger(__name__)


def _validated_mailbox(value: str, *, field: str) -> str:
    candidate = value.strip()
    _display, parsed = parseaddr(candidate)
    if (
        not candidate
        or any(character in candidate for character in ("\r", "\n", "\x00"))
        or parsed != candidate
        or "@" not in parsed
    ):
        raise DeliveryError(f"invalid {field} mailbox", category="recipient_invalid")
    return candidate


def _message_id(brief_id: str) -> str:
    logical_key = f"brief:{brief_id}:channel:email".encode("utf-8")
    digest = hashlib.sha256(logical_key).hexdigest()
    return f"<iic-{digest}@iic-forge.local>"


class EmailOutbound(DeliveryChannel):
    channel_name = "email"

    def send(self, *, brief: Dict[str, Any], mode: str, body: str) -> int:
        if mode in {"event_alert", "event_alert_light"}:
            return super().send(brief=brief, mode=mode, body=body)
        if not self._config["smtp"].get("enabled", False):
            return store.insert_delivery(
                self._conn,
                brief_id=brief["brief_id"],
                channel=self.channel_name,
                status="skipped",
                sent_ts=None,
                channel_ref=None,
                skip_reason="smtp_disabled",
            )
        return super().send(brief=brief, mode=mode, body=body)

    def send_attempt(self, *, brief: Dict[str, Any], mode: str, body: str) -> int:
        if mode not in {"event_alert", "event_alert_light"} and not self._config[
            "smtp"
        ].get("enabled", False):
            return store.insert_delivery(
                self._conn,
                brief_id=brief["brief_id"],
                channel=self.channel_name,
                status="skipped",
                sent_ts=None,
                channel_ref=None,
                skip_reason="smtp_disabled",
            )
        return super().send_attempt(brief=brief, mode=mode, body=body)

    def _send_impl(self, brief: Dict[str, Any], mode: str, body: str) -> tuple:
        smtp_cfg = self._config["smtp"]
        if not smtp_cfg.get("enabled", False):
            raise DeliveryError(
                "email delivery is disabled", category="channel_disabled"
            )
        user = os.environ.get("IIC_SMTP_USER", "")
        pw = os.environ.get("IIC_SMTP_APP_PASSWORD", "")
        if not user or not pw:
            raise DeliveryError(
                "IIC_SMTP_USER / IIC_SMTP_APP_PASSWORD not set",
                category="credential_missing",
            )

        host = str(smtp_cfg.get("host", "")).strip()
        try:
            port = int(smtp_cfg.get("port", 0))
        except (TypeError, ValueError) as exc:
            raise DeliveryError(
                "invalid SMTP port", category="configuration_invalid"
            ) from exc
        if not host or not (1 <= port <= 65535):
            raise DeliveryError(
                "SMTP host/port is not configured correctly",
                category="configuration_invalid",
            )
        from_addr = _validated_mailbox(
            str(smtp_cfg.get("from_addr", "")), field="sender"
        )
        raw_to = smtp_cfg.get("to_addrs") or []
        if not isinstance(raw_to, list) or not raw_to:
            raise DeliveryError(
                "no email recipient configured", category="recipient_missing"
            )
        to_addrs = [
            _validated_mailbox(str(value), field="recipient") for value in raw_to
        ]

        msg = EmailMessage()
        message_id = _message_id(str(brief["brief_id"]))
        msg["Message-ID"] = message_id
        msg["From"] = from_addr
        msg["To"] = ", ".join(to_addrs)
        safe_scope = str(brief.get("scope", "")).replace("\r", " ").replace("\n", " ")
        msg["Subject"] = f"[IIC] {mode}: {safe_scope[:160]}"

        msg.set_content("This message requires an HTML-capable viewer.")
        msg.add_alternative(sanitize_email_html(body), subtype="html")

        smtp = smtplib.SMTP(host, port, timeout=30)
        try:
            smtp.starttls()
            smtp.login(user, pw)
            smtp.send_message(msg)
        except smtplib.SMTPAuthenticationError as exc:
            raise DeliveryError(
                f"SMTP authentication rejected: {exc.smtp_code}",
                category="credential_rejected",
            ) from exc
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            raise DeliveryError(
                f"SMTP sender/recipient rejected: {exc}",
                category="recipient_rejected",
            ) from exc
        except smtplib.SMTPResponseException as exc:
            if 500 <= int(exc.smtp_code) < 600:
                raise DeliveryError(
                    f"SMTP permanently rejected delivery: {exc.smtp_code}",
                    category="smtp_rejected",
                ) from exc
            raise
        except smtplib.SMTPNotSupportedError as exc:
            raise DeliveryError(
                f"SMTP server does not support required operation: {exc}",
                category="configuration_invalid",
            ) from exc
        finally:
            try:
                smtp.quit()
            except smtplib.SMTPException:
                # A QUIT failure cannot retract a message already accepted by
                # the server and must not trigger a duplicate retry.
                log.warning("SMTP QUIT failed after delivery attempt", exc_info=True)

        return (message_id, None)
