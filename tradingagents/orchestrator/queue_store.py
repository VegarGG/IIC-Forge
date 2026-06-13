"""Low-level SQL helpers over the queue_jobs table.

Each function takes an open sqlite3.Connection and commits before returning,
EXCEPT lease_one which relies on the implicit BEGIN IMMEDIATE inside
``with conn:`` for atomicity (and commits at the end of the with-block).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_queue_job(
    conn: sqlite3.Connection,
    *,
    job_type: str,
    payload: str,                      # already-serialized JSON string
    trigger_event_id: Optional[str],
    lane: str = "deep",
    timeout_seconds: Optional[int] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO queue_jobs (job_type, payload, state, enqueued_ts, "
        "trigger_event_id, lane, timeout_seconds) VALUES (?, ?, 'queued', ?, ?, ?, ?)",
        (job_type, payload, _now_iso(), trigger_event_id, lane, timeout_seconds),
    )
    conn.commit()
    return cur.lastrowid


def lease_one(conn: sqlite3.Connection, *, lane: Optional[str] = None) -> Optional[sqlite3.Row]:
    """Atomically claim the oldest queued job. Returns the updated row or None.

    Uses ``UPDATE … RETURNING`` (sqlite >= 3.35). The implicit BEGIN IMMEDIATE
    from ``with conn:`` ensures two concurrent leasers cannot both win the
    same job — the second sees the row already updated and returns nothing.

    When ``lane`` is given, only jobs on that lane are claimed; when None,
    any lane is eligible (preserves pre-lane behavior for existing callers).
    heartbeat_ts is set to the same timestamp as started_ts on claim.
    """
    lane_filter = "AND lane = ?" if lane is not None else ""
    # heartbeat_ts is set once at claim time (same value as started_ts).
    # Periodic in-flight heartbeat updates are future work; the stale-lease
    # sweep (sweep_stale_leases) uses started_ts + per-job timeout_seconds,
    # not heartbeat_ts, to determine staleness.
    params = [_now_iso()]
    if lane is not None:
        params.append(lane)
    with conn:
        row = conn.execute(
            f"""
            UPDATE queue_jobs
               SET state = 'running',
                   started_ts = ?,
                   heartbeat_ts = ?
             WHERE job_id = (
                 SELECT job_id FROM queue_jobs
                  WHERE state = 'queued'
                  {lane_filter}
                  ORDER BY job_id
                  LIMIT 1
             )
         RETURNING job_id, job_type, payload, trigger_event_id, state, started_ts, lane, timeout_seconds
            """,
            (params[0], *params),
        ).fetchone()
    return row


def lane_depth(conn: sqlite3.Connection) -> dict:
    """Return per-lane, per-state job counts for all non-empty combinations.

    Example: {"action": {"queued": 2, "running": 1}, "deep": {"queued": 1}}
    """
    rows = conn.execute(
        "SELECT lane, state, COUNT(*) AS n FROM queue_jobs GROUP BY lane, state"
    ).fetchall()
    out: dict = {}
    for row in rows:
        out.setdefault(row["lane"], {})[row["state"]] = int(row["n"])
    return out


def mark_done(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    run_ids: Iterable[str],
    brief_id: Optional[str],
    cost_usd: Optional[float],
) -> None:
    # NOTE: Updates unconditionally (no WHERE state='running' guard). When an
    # action-lane producer is added with shorter per-job timeouts, a swept job's
    # later mark_done could overwrite the 'error' state set by sweep_stale_leases.
    # Add a WHERE state='running' guard at that point.
    conn.execute(
        "UPDATE queue_jobs SET state = 'done', finished_ts = ?, "
        "run_ids = ?, brief_id = ?, cost_usd = ? WHERE job_id = ?",
        (_now_iso(), json.dumps(list(run_ids)), brief_id, cost_usd, job_id),
    )
    conn.commit()


def mark_error(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    error_msg: str,
) -> None:
    # NOTE: Updates unconditionally (no WHERE state='running' guard). See mark_done
    # comment — add the guard when action-lane shorter timeouts are introduced.
    conn.execute(
        "UPDATE queue_jobs SET state = 'error', finished_ts = ?, error = ? "
        "WHERE job_id = ?",
        (_now_iso(), error_msg, job_id),
    )
    conn.commit()


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
) -> int:
    """Mark any 'running' job older than its per-job timeout as 'error'.

    Used by the worker at boot AND periodically in-loop (S-4) to recover jobs
    left 'running' by an unclean shutdown or a blown wall-clock cap. ``reason``
    is recorded in the error column for post-mortems.
    Returns the number of rows swept.

    Per-job timeout: when ``timeout_seconds`` is set on the row, that value
    governs staleness; ``max_age_seconds`` is the fallback for rows without a
    per-job timeout (COALESCE). This means existing tests that don't set
    ``timeout_seconds`` continue to use the global max_age fallback unchanged.

    NOTE: ``started_ts`` is stored as an ISO-8601 string with a 'T' separator
    and a '+00:00' offset, so it MUST be wrapped in ``datetime(...)`` before
    comparison with SQLite's space-separated ``datetime('now', ?)``. A raw
    string compare silently fails for same-calendar-date rows ('T' 0x54 >
    ' ' 0x20), which made this sweep a no-op for any job that went stale today.
    """
    n = conn.execute(
        "UPDATE queue_jobs SET state = 'error', finished_ts = ?, "
        "error = ? "
        "WHERE state = 'running' "
        "  AND datetime(started_ts) < datetime('now', "
        "        '-' || CAST(COALESCE(timeout_seconds, ?) AS TEXT) || ' seconds')",
        (_now_iso(), reason, max_age_seconds),
    ).rowcount
    conn.commit()
    return n
