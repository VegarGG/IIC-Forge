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


def delivery_idempotency_key(*, brief_id: str, channel: str) -> str:
    """Stable logical-delivery key shared across every transport attempt."""
    return f"brief:{brief_id}:channel:{channel}"


def _record_event(
    conn: sqlite3.Connection,
    *,
    delivery_job_id: int,
    event_type: str,
    from_state: Optional[str],
    to_state: str,
    created_ts: str,
    error_category: Optional[str] = None,
    note: Optional[str] = None,
) -> None:
    conn.execute(
        "INSERT INTO delivery_queue_events "
        "(delivery_job_id, event_type, from_state, to_state, error_category, "
        "note, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            delivery_job_id,
            event_type,
            from_state,
            to_state,
            error_category,
            note[:2000] if note else None,
            created_ts,
        ),
    )


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
    """Persist one outbound/channel intent, returning the stable queue row id."""
    if mode not in {"event_alert", "event_alert_light", "morning_digest"}:
        raise ValueError(f"delivery outbox does not accept mode {mode!r}")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    created = (now or _utc_now()).astimezone(timezone.utc)
    payload = json.dumps(brief_payload, sort_keys=True, separators=(",", ":"))
    available = next_allowed_utc(now_utc=created, config=quiet_hours)
    created_iso = created.isoformat()
    idempotency_key = delivery_idempotency_key(brief_id=brief_id, channel=channel)
    cur = conn.execute(
        "INSERT INTO delivery_queue "
        "(idempotency_key, brief_id, channel, mode, brief_payload, body, state, attempt_count, "
        "max_attempts, available_ts, created_ts, updated_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?) "
        "ON CONFLICT(idempotency_key) DO NOTHING",
        (
            idempotency_key,
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
        _record_event(
            conn,
            delivery_job_id=delivery_job_id,
            event_type="enqueued",
            from_state=None,
            to_state="queued",
            created_ts=created_iso,
        )
    else:
        existing = conn.execute(
            "SELECT delivery_job_id, mode, brief_payload, body "
            "FROM delivery_queue WHERE idempotency_key = ?",
            (idempotency_key,),
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
    with conn:
        changed = conn.execute(
            "UPDATE delivery_queue SET state = 'sent', last_delivery_id = ?, "
            "last_error = NULL, error_category = NULL, blocked_ts = NULL, "
            "lease_token = NULL, lease_expires_ts = NULL, updated_ts = ? "
            "WHERE delivery_job_id = ? AND state = 'running' AND lease_token = ?",
            (delivery_id, finished, delivery_job_id, lease_token),
        ).rowcount
        if changed != 1:
            raise DeliveryLeaseLost(
                f"delivery job {delivery_job_id} is no longer owned by this worker"
            )
        _record_event(
            conn,
            delivery_job_id=delivery_job_id,
            event_type="sent",
            from_state="running",
            to_state="sent",
            created_ts=finished,
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
    error_category: str = "transport_error",
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
        if not retryable:
            state = "blocked"
            event_type = "blocked"
            blocked_ts: Optional[str] = failed.isoformat()
            available = failed
        elif exhausted:
            state = "dead"
            event_type = "attempts_exhausted"
            blocked_ts = None
            available = failed
        else:
            state = "queued"
            event_type = "retry_scheduled"
            blocked_ts = None
            exponent = max(int(row["attempt_count"]) - 1, 0)
            delay = min(retry_base_seconds * (2**exponent), retry_cap_seconds)
            available = failed + timedelta(seconds=delay)
        changed = conn.execute(
            "UPDATE delivery_queue SET state = ?, available_ts = ?, "
            "last_error = ?, last_error_ts = ?, last_delivery_id = ?, "
            "error_category = ?, blocked_ts = ?, "
            "lease_token = NULL, lease_expires_ts = NULL, updated_ts = ? "
            "WHERE delivery_job_id = ? AND state = 'running' AND lease_token = ?",
            (
                state,
                available.isoformat(),
                error[:2000],
                failed.isoformat(),
                delivery_id,
                error_category,
                blocked_ts,
                failed.isoformat(),
                delivery_job_id,
                lease_token,
            ),
        ).rowcount
        if changed != 1:
            raise DeliveryLeaseLost(
                f"delivery job {delivery_job_id} is no longer owned by this worker"
            )
        _record_event(
            conn,
            delivery_job_id=delivery_job_id,
            event_type=event_type,
            from_state="running",
            to_state=state,
            error_category=error_category,
            note=error,
            created_ts=failed.isoformat(),
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
    current_iso = current.isoformat()
    with conn:
        changed = conn.execute(
            "UPDATE delivery_queue SET state = 'queued', available_ts = ?, "
            "attempt_count = CASE WHEN attempt_count > 0 THEN attempt_count - 1 ELSE 0 END, "
            "lease_token = NULL, lease_expires_ts = NULL, updated_ts = ? "
            "WHERE delivery_job_id = ? AND state = 'running' AND lease_token = ?",
            (available.isoformat(), current_iso, delivery_job_id, lease_token),
        ).rowcount
        if changed != 1:
            raise DeliveryLeaseLost(
                f"delivery job {delivery_job_id} is no longer owned by this worker"
            )
        _record_event(
            conn,
            delivery_job_id=delivery_job_id,
            event_type="quiet_hours_deferred",
            from_state="running",
            to_state="queued",
            created_ts=current_iso,
            note=f"deferred until {available.isoformat()}",
        )


def sweep_expired_leases(
    conn: sqlite3.Connection,
    *,
    now: Optional[datetime] = None,
    reason: str = "delivery_lease_expired",
) -> int:
    current = (now or _utc_now()).astimezone(timezone.utc).isoformat()
    with conn:
        expired = list(
            conn.execute(
                "SELECT delivery_job_id, attempt_count, max_attempts "
                "FROM delivery_queue WHERE state = 'running' "
                "AND datetime(lease_expires_ts) <= datetime(?)",
                (current,),
            )
        )
        for row in expired:
            state = (
                "dead"
                if int(row["attempt_count"]) >= int(row["max_attempts"])
                else "queued"
            )
            changed = conn.execute(
                "UPDATE delivery_queue SET state = ?, available_ts = ?, "
                "last_error = ?, last_error_ts = ?, error_category = 'lease_expired', "
                "lease_token = NULL, lease_expires_ts = NULL, updated_ts = ? "
                "WHERE delivery_job_id = ? AND state = 'running' "
                "AND datetime(lease_expires_ts) <= datetime(?)",
                (
                    state,
                    current,
                    reason,
                    current,
                    current,
                    row["delivery_job_id"],
                    current,
                ),
            ).rowcount
            if changed:
                _record_event(
                    conn,
                    delivery_job_id=int(row["delivery_job_id"]),
                    event_type=(
                        "attempts_exhausted" if state == "dead" else "lease_recovered"
                    ),
                    from_state="running",
                    to_state=state,
                    error_category="lease_expired",
                    note=reason,
                    created_ts=current,
                )
    return len(expired)


def retry_dead(
    conn: sqlite3.Connection,
    *,
    delivery_job_id: int,
    operator_note: str,
    additional_attempts: int = 1,
    now: Optional[datetime] = None,
) -> bool:
    """Operator-controlled replay of one dead delivery intent."""
    if additional_attempts < 1:
        raise ValueError("additional_attempts must be at least 1")
    if not operator_note.strip():
        raise ValueError("operator_note must not be empty")
    available = (now or _utc_now()).astimezone(timezone.utc).isoformat()
    with conn:
        changed = conn.execute(
            "UPDATE delivery_queue SET state = 'queued', available_ts = ?, "
            "max_attempts = attempt_count + ?, operator_note = ?, "
            "lease_token = NULL, lease_expires_ts = NULL, updated_ts = ? "
            "WHERE delivery_job_id = ? AND state = 'dead'",
            (
                available,
                additional_attempts,
                operator_note[:2000],
                available,
                delivery_job_id,
            ),
        ).rowcount
        if changed:
            _record_event(
                conn,
                delivery_job_id=delivery_job_id,
                event_type="operator_retry",
                from_state="dead",
                to_state="queued",
                note=operator_note,
                created_ts=available,
            )
    return changed == 1


def requeue_blocked(
    conn: sqlite3.Connection,
    *,
    delivery_job_id: int,
    operator_note: str,
    additional_attempts: int = 1,
    now: Optional[datetime] = None,
) -> bool:
    """Requeue a permanently blocked intent after its cause is corrected."""
    if additional_attempts < 1:
        raise ValueError("additional_attempts must be at least 1")
    if not operator_note.strip():
        raise ValueError("operator_note must not be empty")
    available = (now or _utc_now()).astimezone(timezone.utc).isoformat()
    with conn:
        changed = conn.execute(
            "UPDATE delivery_queue SET state = 'queued', available_ts = ?, "
            "max_attempts = MAX(max_attempts, attempt_count + ?), "
            "operator_note = ?, blocked_ts = NULL, lease_token = NULL, "
            "lease_expires_ts = NULL, updated_ts = ? "
            "WHERE delivery_job_id = ? AND state = 'blocked'",
            (
                available,
                additional_attempts,
                operator_note[:2000],
                available,
                delivery_job_id,
            ),
        ).rowcount
        if changed:
            _record_event(
                conn,
                delivery_job_id=delivery_job_id,
                event_type="operator_requeue",
                from_state="blocked",
                to_state="queued",
                note=operator_note,
                created_ts=available,
            )
    return changed == 1


def cancel_delivery(
    conn: sqlite3.Connection,
    *,
    delivery_job_id: int,
    operator_note: str,
    now: Optional[datetime] = None,
) -> bool:
    """Cancel an inactive intent; running/sent work is intentionally refused."""
    if not operator_note.strip():
        raise ValueError("operator_note must not be empty")
    cancelled = (now or _utc_now()).astimezone(timezone.utc).isoformat()
    with conn:
        row = conn.execute(
            "SELECT state FROM delivery_queue WHERE delivery_job_id = ? ",
            (delivery_job_id,),
        ).fetchone()
        if row is None or row["state"] not in {"queued", "blocked", "dead"}:
            return False
        from_state = str(row["state"])
        changed = conn.execute(
            "UPDATE delivery_queue SET state = 'cancelled', cancelled_ts = ?, "
            "operator_note = ?, lease_token = NULL, lease_expires_ts = NULL, "
            "updated_ts = ? WHERE delivery_job_id = ? AND state = ?",
            (
                cancelled,
                operator_note[:2000],
                cancelled,
                delivery_job_id,
                from_state,
            ),
        ).rowcount
        if changed:
            _record_event(
                conn,
                delivery_job_id=delivery_job_id,
                event_type="operator_cancel",
                from_state=from_state,
                to_state="cancelled",
                note=operator_note,
                created_ts=cancelled,
            )
    return changed == 1


def inspect_delivery(
    conn: sqlite3.Connection, *, delivery_job_id: int
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    row = conn.execute(
        "SELECT * FROM delivery_queue WHERE delivery_job_id = ?",
        (delivery_job_id,),
    ).fetchone()
    if row is None:
        return None, []
    events = conn.execute(
        "SELECT * FROM delivery_queue_events WHERE delivery_job_id = ? "
        "ORDER BY event_id",
        (delivery_job_id,),
    ).fetchall()
    return dict(row), [dict(event) for event in events]
