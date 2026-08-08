"""Low-level SQL helpers over the queue_jobs table.

Jobs are delivered at least once. Enqueue is optionally idempotent, leases are
fenced with an opaque token, failures retry with bounded exponential backoff,
and exhausted jobs remain visible in the terminal ``error`` state.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional


class QueueLeaseLost(RuntimeError):
    """A worker attempted to mutate a job after losing its lease."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_queue_job(
    conn: sqlite3.Connection,
    *,
    job_type: str,
    payload: str,                      # already-serialized JSON string
    trigger_event_id: Optional[str],
    idempotency_key: Optional[str] = None,
    max_attempts: int = 3,
    available_ts: Optional[str] = None,
    commit: bool = True,
) -> int:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("queue job payload must be a JSON object")
    now = _now_iso()
    cur = conn.execute(
        "INSERT INTO queue_jobs (job_type, payload, state, enqueued_ts, "
        "trigger_event_id, idempotency_key, max_attempts, available_ts) "
        "VALUES (?, ?, 'queued', ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
        (
            job_type,
            payload,
            now,
            trigger_event_id,
            idempotency_key,
            max_attempts,
            available_ts or now,
        ),
    )
    if cur.rowcount == 1:
        row_id = cur.lastrowid
        if row_id is None:
            raise RuntimeError("queue insert completed without a row identifier")
        job_id = int(row_id)
    elif idempotency_key is not None:
        existing = conn.execute(
            "SELECT job_id, job_type, payload, trigger_event_id "
            "FROM queue_jobs WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is None:
            raise RuntimeError("idempotent queue insert was ignored without a match")
        if (
            existing["job_type"] != job_type
            or existing["payload"] != payload
            or existing["trigger_event_id"] != trigger_event_id
        ):
            raise ValueError(
                f"idempotency key {idempotency_key!r} already identifies a "
                "different queue job"
            )
        job_id = int(existing["job_id"])
    else:
        raise RuntimeError("queue insert was unexpectedly ignored")
    if commit:
        conn.commit()
    return job_id


def lease_one(
    conn: sqlite3.Connection,
    *,
    lease_seconds: int = 1500,
    now: Optional[datetime] = None,
) -> Optional[sqlite3.Row]:
    """Atomically claim the oldest queued job. Returns the updated row or None.

    Uses ``UPDATE … RETURNING`` (sqlite >= 3.35). The implicit BEGIN IMMEDIATE
    from ``with conn:`` ensures two concurrent leasers cannot both win the
    same job — the second sees the row already updated and returns nothing.
    """
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be at least 1")
    claimed_at = now or datetime.now(timezone.utc)
    claimed_iso = claimed_at.isoformat()
    lease_token = uuid.uuid4().hex
    lease_expires = (claimed_at + timedelta(seconds=lease_seconds)).isoformat()
    with conn:
        row = conn.execute(
            """
            UPDATE queue_jobs
               SET state = 'running',
                   started_ts = ?,
                   finished_ts = NULL,
                   lease_token = ?,
                   lease_expires_ts = ?,
                   attempt_count = attempt_count + 1
             WHERE job_id = (
                 SELECT job_id FROM queue_jobs
                  WHERE state = 'queued'
                    AND datetime(COALESCE(available_ts, enqueued_ts))
                        <= datetime(?)
                  ORDER BY job_id
                  LIMIT 1
             )
         RETURNING job_id, job_type, payload, trigger_event_id, state,
                   started_ts, attempt_count, max_attempts, lease_token,
                   lease_expires_ts, idempotency_key
            """,
            (claimed_iso, lease_token, lease_expires, claimed_iso),
        ).fetchone()
    return row


def mark_done(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    run_ids: Iterable[str],
    brief_id: Optional[str],
    cost_usd: Optional[float],
    lease_token: Optional[str] = None,
) -> None:
    sql = (
        "UPDATE queue_jobs SET state = 'done', finished_ts = ?, "
        "run_ids = ?, brief_id = ?, cost_usd = ?, error = NULL, "
        "lease_token = NULL, lease_expires_ts = NULL WHERE job_id = ? "
        "AND state = 'running'"
    )
    params: tuple = (
        _now_iso(), json.dumps(list(run_ids)), brief_id, cost_usd, job_id,
    )
    if lease_token is not None:
        # The opaque token is the fence. A worker may finish just after the
        # nominal expiry as long as no sweeper/new worker has replaced its
        # lease. This ordering avoids unnecessary duplicate side effects at
        # the lease boundary while still rejecting every superseded worker.
        sql += " AND lease_token = ?"
        params += (lease_token,)
    changed = conn.execute(sql, params).rowcount
    conn.commit()
    if changed != 1:
        raise QueueLeaseLost(f"queue job {job_id} is no longer owned by this worker")


def mark_failure(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    error_msg: str,
    lease_token: str,
    retry_base_seconds: int = 30,
    retry_cap_seconds: int = 900,
    now: Optional[datetime] = None,
) -> str:
    """Record a failed attempt and return the resulting state.

    The job is re-queued until ``max_attempts`` is reached. The lease token is
    mandatory so an expired worker cannot overwrite a newer attempt.
    """
    if retry_base_seconds < 0 or retry_cap_seconds < 0:
        raise ValueError("retry delays cannot be negative")
    failed_at = now or datetime.now(timezone.utc)
    with conn:
        row = conn.execute(
            "SELECT attempt_count, max_attempts FROM queue_jobs "
            "WHERE job_id = ? AND state = 'running' AND lease_token = ?",
            (job_id, lease_token),
        ).fetchone()
        if row is None:
            raise QueueLeaseLost(
                f"queue job {job_id} is no longer owned by this worker"
            )
        exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
        if exhausted:
            state = "error"
            available_ts = failed_at.isoformat()
            finished_ts: Optional[str] = failed_at.isoformat()
        else:
            state = "queued"
            exponent = max(int(row["attempt_count"]) - 1, 0)
            delay = min(retry_base_seconds * (2 ** exponent), retry_cap_seconds)
            available_ts = (failed_at + timedelta(seconds=delay)).isoformat()
            finished_ts = None
        changed = conn.execute(
            "UPDATE queue_jobs SET state = ?, available_ts = ?, finished_ts = ?, "
            "error = ?, last_error_ts = ?, lease_token = NULL, "
            "lease_expires_ts = NULL WHERE job_id = ? AND state = 'running' "
            "AND lease_token = ?",
            (
                state,
                available_ts,
                finished_ts,
                error_msg[:2000],
                failed_at.isoformat(),
                job_id,
                lease_token,
            ),
        ).rowcount
        if changed != 1:
            raise QueueLeaseLost(
                f"queue job {job_id} is no longer owned by this worker"
            )
    return state


def mark_error(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    error_msg: str,
) -> None:
    """Administrative terminal failure retained for backwards compatibility."""
    changed = conn.execute(
        "UPDATE queue_jobs SET state = 'error', finished_ts = ?, error = ?, "
        "last_error_ts = ?, lease_token = NULL, lease_expires_ts = NULL "
        "WHERE job_id = ? AND state IN ('queued', 'running')",
        (_now_iso(), error_msg[:2000], _now_iso(), job_id),
    ).rowcount
    conn.commit()
    if changed != 1:
        raise QueueLeaseLost(f"queue job {job_id} is not mutable")


def pending_count(conn: sqlite3.Connection) -> int:
    """Jobs currently queued OR running (anything not yet terminal)."""
    return conn.execute(
        "SELECT COUNT(*) FROM queue_jobs WHERE state IN ('queued', 'running')"
    ).fetchone()[0]


def daily_enqueue_count(conn: sqlite3.Connection) -> int:
    """Jobs enqueued in the last 24h (regardless of current state)."""
    return conn.execute(
        "SELECT COUNT(*) FROM queue_jobs "
        "WHERE datetime(enqueued_ts) > datetime('now', '-1 day')"
    ).fetchone()[0]


def daily_cost_total(conn: sqlite3.Connection) -> float:
    """Sum of cost_usd for jobs finished today (UTC date)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM queue_jobs "
        "WHERE state = 'done' AND date(finished_ts) = date('now')"
    ).fetchone()
    return float(row[0])


def sweep_stale_leases(
    conn: sqlite3.Connection, *, max_age_seconds: int = 3600,
    reason: str = "stale_lease_swept_on_boot",
    now: Optional[datetime] = None,
) -> int:
    """Recover expired leases, re-queueing until attempts are exhausted.

    Used by the worker at boot AND periodically in-loop (S-4) to recover jobs
    left 'running' by an unclean shutdown or a blown wall-clock cap. ``reason``
    is recorded in the error column for post-mortems.
    Returns the number of rows swept.

    NOTE: ``started_ts`` is stored as an ISO-8601 string with a 'T' separator
    and a '+00:00' offset, so it MUST be wrapped in ``datetime(...)`` before
    comparison with SQLite's space-separated ``datetime('now', ?)``. A raw
    string compare silently fails for same-calendar-date rows ('T' 0x54 >
    ' ' 0x20), which made this sweep a no-op for any job that went stale today.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = current.isoformat()
    age_cutoff = (current - timedelta(seconds=max_age_seconds)).isoformat()
    n = conn.execute(
        "UPDATE queue_jobs SET "
        "state = CASE WHEN attempt_count >= max_attempts THEN 'error' ELSE 'queued' END, "
        "finished_ts = CASE WHEN attempt_count >= max_attempts THEN ? ELSE NULL END, "
        "available_ts = ?, error = ?, last_error_ts = ?, "
        "lease_token = NULL, lease_expires_ts = NULL "
        "WHERE state = 'running' AND ("
        "(lease_expires_ts IS NOT NULL AND datetime(lease_expires_ts) <= datetime(?)) "
        "OR datetime(started_ts) <= datetime(?)"
        ")",
        (now_iso, now_iso, reason, now_iso, now_iso, age_cutoff),
    ).rowcount
    conn.commit()
    return n


def retry_error_job(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    additional_attempts: int = 1,
    now: Optional[datetime] = None,
) -> bool:
    """Operator-controlled replay of one terminal job."""
    if additional_attempts < 1:
        raise ValueError("additional_attempts must be at least 1")
    available = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    changed = conn.execute(
        "UPDATE queue_jobs SET state = 'queued', available_ts = ?, "
        "finished_ts = NULL, max_attempts = attempt_count + ?, "
        "lease_token = NULL, lease_expires_ts = NULL "
        "WHERE job_id = ? AND state = 'error'",
        (available.isoformat(), additional_attempts, job_id),
    ).rowcount
    conn.commit()
    return changed == 1
