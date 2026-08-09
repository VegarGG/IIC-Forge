"""DeliveryChannel base class.

Every channel inherits from DeliveryChannel and implements ``_send_impl``.
The base ``send()`` routes every automatic outbound into the durable outbox. The
delivery worker calls ``send_attempt()`` which handles:
  - a final quiet-hours check
  - writing the deliveries row on success / failure / skip
  - returning the delivery_id

A channel's ``_send_impl`` returns a tuple ``(channel_ref, error_msg)``:
  - on success: (channel_ref, None)
  - on failure: it should raise; the base catches and records the message
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict

from tradingagents.delivery.quiet_hours import is_quiet_hours
from tradingagents.persistence import store


class DeliveryError(Exception):
    """A permanent transport/configuration failure requiring operator action."""

    def __init__(self, message: str, *, category: str = "configuration_error") -> None:
        super().__init__(message)
        self.category = category


_QUEUED_MODES = {"event_alert", "event_alert_light", "morning_digest"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_now(timezone_name: str = "Asia/Shanghai") -> time:
    """Configured-zone local time used for quiet-hours comparison."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(timezone_name)).time().replace(tzinfo=None)


class DeliveryChannel(ABC):
    channel_name: str = "abstract"

    def __init__(self, *, conn: sqlite3.Connection, config: Dict[str, Any]) -> None:
        self._conn = conn
        self._config = config

    @abstractmethod
    def _send_impl(self, brief: Dict[str, Any], mode: str, body: str) -> tuple:
        """Return (channel_ref, error_msg). Raise on failure."""

    def send(self, *, brief: Dict[str, Any], mode: str, body: str) -> int:
        if mode in _QUEUED_MODES:
            from tradingagents.delivery import queue_store

            return queue_store.enqueue_alert(
                self._conn,
                brief_id=brief["brief_id"],
                channel=self.channel_name,
                mode=mode,
                brief_payload=brief,
                body=body,
                quiet_hours=self._config["delivery"]["quiet_hours"],
                max_attempts=int(self._config["delivery"].get("queue_max_attempts", 5)),
            )
        return self.send_attempt(brief=brief, mode=mode, body=body)

    def send_attempt(self, *, brief: Dict[str, Any], mode: str, body: str) -> int:
        """Attempt transport now and append one immutable delivery audit row."""
        if mode in _QUEUED_MODES and is_quiet_hours(
            local_time=_local_now(
                self._config["delivery"]["quiet_hours"].get("timezone", "Asia/Shanghai")
            ),
            config=self._config["delivery"]["quiet_hours"],
        ):
            return store.insert_delivery(
                self._conn,
                brief_id=brief["brief_id"],
                channel=self.channel_name,
                status="skipped",
                sent_ts=None,
                channel_ref=None,
                skip_reason="quiet_hours",
            )

        try:
            channel_ref, _err = self._send_impl(brief, mode, body)
            delivery_id = store.insert_delivery(
                self._conn,
                brief_id=brief["brief_id"],
                channel=self.channel_name,
                status="sent",
                sent_ts=_utc_now_iso(),
                channel_ref=channel_ref,
                skip_reason=None,
            )
            # S-8: on event_alert delivery, create EXACTLY ONE pending
            # brief_action (matching the [Run Backtest]/[Dismiss] keyboard) so
            # an ignored alert can lapse to 'expired' organically (gate G5).
            # base.send() is per-channel, so guard on existing rows for this
            # brief_id — re-delivery or multiple channels create no duplicates.
            if mode == "event_alert":
                self._ensure_pending_action(brief["brief_id"])
            return delivery_id
        except DeliveryError as exc:
            return store.insert_delivery(
                self._conn,
                brief_id=brief["brief_id"],
                channel=self.channel_name,
                status="blocked",
                sent_ts=None,
                channel_ref=str(exc)[:500],
                skip_reason=exc.category,
            )
        except Exception as exc:  # noqa: BLE001
            return store.insert_delivery(
                self._conn,
                brief_id=brief["brief_id"],
                channel=self.channel_name,
                status="failed",
                sent_ts=None,
                channel_ref=str(exc)[:500],
                skip_reason=None,
            )

    def _ensure_pending_action(self, brief_id: str) -> None:
        """Idempotently create one pending 'run_backtest' brief_action for an
        event_alert brief. No-op if any action already exists for this brief."""
        if store.count_brief_actions(self._conn, brief_id=brief_id) > 0:
            return
        ttl_hours = self._config.get("brief_action_ttl_hours", 24)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        ).isoformat()
        store.insert_brief_action(
            self._conn,
            brief_id=brief_id,
            action_type="run_backtest",
            action_params={},
            expires_at=expires_at,
        )
