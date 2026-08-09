"""Queue panel — current depth + recent jobs + worker heartbeat."""

from __future__ import annotations

import sqlite3
from typing import Optional


def fetch_queue_depth(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM queue_jobs GROUP BY state"
    ).fetchall()
    return {r["state"]: r["n"] for r in rows}


def fetch_recent_jobs(conn: sqlite3.Connection, *, limit: int = 10) -> list[dict]:
    from tradingagents.ops.logging import redact

    rows = conn.execute(
        "SELECT job_id, job_type, state, enqueued_ts, started_ts, finished_ts, "
        "attempt_count, max_attempts, available_ts, brief_id, cost_usd, "
        "error_category, error, operator_note FROM queue_jobs "
        "ORDER BY job_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    result = []
    for row in rows:
        value = dict(row)
        for key in ("error", "operator_note"):
            if value.get(key):
                value[key] = redact(value[key])
        result.append(value)
    return result


def fetch_worker_heartbeat(conn: sqlite3.Connection) -> Optional[str]:
    """Explicit analysis-worker process heartbeat, never inferred from work."""
    row = conn.execute(
        "SELECT heartbeat_ts AS last_seen FROM service_heartbeats "
        "WHERE service_name='analysis-worker'"
    ).fetchone()
    return row["last_seen"] if row else None
