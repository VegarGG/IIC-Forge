"""Durable SQLite outbox for alert delivery.

Queue rows are mutable lifecycle records. The existing ``deliveries`` table is
the immutable per-attempt audit trail written by channel implementations.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from tradingagents.delivery.quiet_hours import next_allowed_utc


class DeliveryLeaseLost(RuntimeError):
    """A delivery worker attempted to mutate an expired/replaced lease."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def enqueue_alert(
    conn: sqlite3.Connection,
    *,
    brief_id: str,
    channel: str,
    mode: str,
    brief_payload: dict[str, Any],
    body: str,
    quiet_hours: dict,
    max_attempts: int = 5,
    now: Optional[datetime] = None,
    commit: bool = True,
) -> int:
    """Persist one alert/channel intent, returning the stable queue row id."""
    if mode not in {"event_alert", "event_alert_light"}:
        raise ValueError(f"delivery outbox only accepts alert modes, got {mode!r}")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    created = (now or _utc_now()).astimezone(timezone.utc)
    payload = json.dumps(brief_payload, sort_keys=True, separators=(",", ":"))
    available = next_allowed_utc(now_utc=created, config=quiet_hours)
    created_iso = created.isoformat()
    cur = conn.execute(
        "INSERT INTO delivery_queue "
        "(brief_id, channel, mode, brief_payload, body, state, attempt_count, "
        "max_attempts, available_ts, created_ts, updated_ts) "
        "VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?) "
        "ON CONFLICT(brief_id, channel) DO NOTHING",
        (
            brief_id,
            channel,
            mode,
            payload,
            body,
            max_attempts,
            available.isoformat(),
            created_iso,
            created_iso,
        ),
    )
    if cur.rowcount == 1:
        if cur.lastrowid is None:
            raise RuntimeError("delivery enqueue completed without a row identifier")
        delivery_job_id = int(cur.lastrowid)
    else:
        existing = conn.execute(
            "SELECT delivery_job_id, mode, brief_payload, body "
            "FROM delivery_queue WHERE brief_id = ? AND channel = ?",
            (brief_id, channel),
        ).fetchone()
        if existing is None:
            raise RuntimeError("delivery enqueue was ignored without a matching row")
        if (
            existing["mode"] != mode
            or existing["brief_payload"] != payload
            or existing["body"] != body
        ):
            raise ValueError(
                f"delivery intent for brief={brief_id!r} channel={channel!r} "
                "already exists with different content"
            )
        delivery_job_id = int(existing["delivery_job_id"])
    if commit:
        conn.commit()
    return delivery_job_id


def lease_one(
    conn: sqlite3.Connection,
    *,
    lease_seconds: int = 120,
    now: Optional[datetime] = None,
) -> Optional[sqlite3.Row]:
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be at least 1")
    leased_at = (now or _utc_now()).astimezone(timezone.utc)
    token = uuid.uuid4().hex
    expires = leased_at + timedelta(seconds=lease_seconds)
    with conn:
        return conn.execute(
            """
            UPDATE delivery_queue
               SET state = 'running',
                   attempt_count = attempt_count + 1,
                   lease_token = ?,
                   lease_expires_ts = ?,
                   updated_ts = ?
             WHERE delivery_job_id = (
                 SELECT delivery_job_id FROM delivery_queue
                  WHERE state = 'queued'
                    AND datetime(available_ts) <= datetime(?)
                  ORDER BY available_ts, delivery_job_id
                  LIMIT 1
             )
         RETURNING *
            """,
            (token, expires.isoformat(), leased_at.isoformat(), leased_at.isoformat()),
        ).fetchone()


def mark_sent(
    conn: sqlite3.Connection,
    *,
    delivery_job_id: int,
    lease_token: str,
    delivery_id: int,
    now: Optional[datetime] = None,
) -> None:
    finished = (now or _utc_now()).astimezone(timezone.utc).isoformat()
    changed = conn.execute(
        "UPDATE delivery_queue SET state = 'sent', last_delivery_id = ?, "
        "last_error = NULL, lease_token = NULL, lease_expires_ts = NULL, "
        "updated_ts = ? WHERE delivery_job_id = ? AND state = 'running' "
        "AND lease_token = ?",
        (delivery_id, finished, delivery_job_id, lease_token),
    ).rowcount
    conn.commit()
    if changed != 1:
        raise DeliveryLeaseLost(
            f"delivery job {delivery_job_id} is no longer owned by this worker"
        )


def mark_failure(
    conn: sqlite3.Connection,
    *,
    delivery_job_id: int,
    lease_token: str,
    error: str,
    delivery_id: Optional[int],
    retry_base_seconds: int = 30,
    retry_cap_seconds: int = 1800,
    retryable: bool = True,
    now: Optional[datetime] = None,
) -> str:
    failed = (now or _utc_now()).astimezone(timezone.utc)
    with conn:
        row = conn.execute(
            "SELECT attempt_count, max_attempts FROM delivery_queue "
            "WHERE delivery_job_id = ? AND state = 'running' AND lease_token = ?",
            (delivery_job_id, lease_token),
        ).fetchone()
        if row is None:
            raise DeliveryLeaseLost(
                f"delivery job {delivery_job_id} is no longer owned by this worker"
            )
        exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
        if not retryable or exhausted:
            state = "dead"
            available = failed
        else:
            state = "queued"
            exponent = max(int(row["attempt_count"]) - 1, 0)
            delay = min(retry_base_seconds * (2 ** exponent), retry_cap_seconds)
            available = failed + timedelta(seconds=delay)
        changed = conn.execute(
            "UPDATE delivery_queue SET state = ?, available_ts = ?, "
            "last_error = ?, last_error_ts = ?, last_delivery_id = ?, "
            "lease_token = NULL, lease_expires_ts = NULL, updated_ts = ? "
            "WHERE delivery_job_id = ? AND state = 'running' AND lease_token = ?",
            (
                state,
                available.isoformat(),
                error[:2000],
                failed.isoformat(),
                delivery_id,
                failed.isoformat(),
                delivery_job_id,
                lease_token,
            ),
        ).rowcount
        if changed != 1:
            raise DeliveryLeaseLost(
                f"delivery job {delivery_job_id} is no longer owned by this worker"
            )
    return state


def defer_for_quiet_hours(
    conn: sqlite3.Connection,
    *,
    delivery_job_id: int,
    lease_token: str,
    quiet_hours: dict,
    now: Optional[datetime] = None,
) -> None:
    current = (now or _utc_now()).astimezone(timezone.utc)
    available = next_allowed_utc(now_utc=current, config=quiet_hours)
    changed = conn.execute(
        "UPDATE delivery_queue SET state = 'queued', available_ts = ?, "
        "attempt_count = CASE WHEN attempt_count > 0 THEN attempt_count - 1 ELSE 0 END, "
        "lease_token = NULL, lease_expires_ts = NULL, updated_ts = ? "
        "WHERE delivery_job_id = ? AND state = 'running' AND lease_token = ?",
        (available.isoformat(), current.isoformat(), delivery_job_id, lease_token),
    ).rowcount
    conn.commit()
    if changed != 1:
        raise DeliveryLeaseLost(
            f"delivery job {delivery_job_id} is no longer owned by this worker"
        )


def sweep_expired_leases(
    conn: sqlite3.Connection,
    *,
    now: Optional[datetime] = None,
    reason: str = "delivery_lease_expired",
) -> int:
    current = (now or _utc_now()).astimezone(timezone.utc).isoformat()
    changed = conn.execute(
        "UPDATE delivery_queue SET "
        "state = CASE WHEN attempt_count >= max_attempts THEN 'dead' ELSE 'queued' END, "
        "available_ts = ?, last_error = ?, last_error_ts = ?, "
        "lease_token = NULL, lease_expires_ts = NULL, updated_ts = ? "
        "WHERE state = 'running' AND datetime(lease_expires_ts) <= datetime(?)",
        (current, reason, current, current, current),
    ).rowcount
    conn.commit()
    return changed


def retry_dead(
    conn: sqlite3.Connection,
    *,
    delivery_job_id: int,
    additional_attempts: int = 1,
    now: Optional[datetime] = None,
) -> bool:
    """Operator-controlled replay of one dead delivery intent."""
    if additional_attempts < 1:
        raise ValueError("additional_attempts must be at least 1")
    available = (now or _utc_now()).astimezone(timezone.utc).isoformat()
    changed = conn.execute(
        "UPDATE delivery_queue SET state = 'queued', available_ts = ?, "
        "max_attempts = attempt_count + ?, lease_token = NULL, "
        "lease_expires_ts = NULL, updated_ts = ? "
        "WHERE delivery_job_id = ? AND state = 'dead'",
        (available, additional_attempts, available, delivery_job_id),
    ).rowcount
    conn.commit()
    return changed == 1
