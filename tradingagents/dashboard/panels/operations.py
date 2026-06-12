"""Operational status queries shared by dashboard and focused gates.

No streamlit import at module level — this module is imported headlessly by
the focused soak gate as well as the dashboard.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from tradingagents.persistence import store
from tradingagents.orchestrator import queue_store
from tradingagents.dashboard.panels.costs import fetch_provider_split


def _dt(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _age_seconds(now_ts: str, ts: str | None) -> float | None:
    now = _dt(now_ts)
    other = _dt(ts)
    if now is None or other is None:
        return None
    return (now - other).total_seconds()


def fetch_llm_role_summary(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Aggregate LLM call stats grouped by role."""
    rows = conn.execute(
        "SELECT role, COUNT(*) AS total, "
        "SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success, "
        "SUM(CASE WHEN status = 'parse_error' THEN 1 ELSE 0 END) AS parse_failures, "
        "SUM(CASE WHEN status IN ('transport_error', 'timeout') THEN 1 ELSE 0 END) AS transport_failures, "
        "SUM(CASE WHEN fallback_used = 1 THEN 1 ELSE 0 END) AS fallback_used, "
        "AVG(latency_ms) AS avg_latency_ms "
        "FROM llm_calls GROUP BY role"
    ).fetchall()
    return {r["role"]: dict(r) for r in rows}


def fetch_deferred_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return per-state counts for deferred_salience_retry plus orphaned event count.

    Amendment B: an orphaned event is one with salience_source = 'deferred' and
    salience IS NULL where no retry row exists in state pending/running/done.
    A successfully-retried event's original deferred row keeps salience NULL
    while its retry row transitions to 'done', so 'done' is included in the
    NOT EXISTS clause to avoid counting recovered events as orphans.
    """
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM deferred_salience_retry GROUP BY state"
    ).fetchall()
    by_state: dict[str, Any] = {r["state"]: int(r["n"]) for r in rows}

    orphan_row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM events e
        WHERE e.salience_source = 'deferred' AND e.salience IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM deferred_salience_retry r
              WHERE r.event_id = e.event_id
                AND r.state IN ('pending', 'running', 'done')
          )
        """
    ).fetchone()
    by_state["orphaned_events"] = int(orphan_row["cnt"]) if orphan_row else 0

    return by_state


def fetch_failed_delivery_groups(conn: sqlite3.Connection) -> list[dict]:
    """Return groups where no delivery was sent AND at least one attempt failed.

    Amendment A: a quiet-hours-skipped group (sent=0, all skipped by policy)
    is NOT a failed group — it must have at least one status='failed' attempt
    to count here.
    """
    rows = conn.execute(
        """
        SELECT delivery_group_id,
               COUNT(*) AS attempts,
               SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
               SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_attempts
        FROM deliveries
        WHERE delivery_group_id IS NOT NULL
        GROUP BY delivery_group_id
        HAVING sent = 0 AND failed_attempts >= 1
        """
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_skipped_only_delivery_group_count(conn: sqlite3.Connection) -> int:
    """Count groups where sent=0 and no failed attempt exists (all skipped by policy).

    Amendment A: these are intentionally not alarms — they're policy-skipped groups.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM (
            SELECT delivery_group_id,
                   SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_attempts
            FROM deliveries
            WHERE delivery_group_id IS NOT NULL
            GROUP BY delivery_group_id
            HAVING sent = 0 AND failed_attempts = 0
        )
        """
    ).fetchone()
    return int(row["cnt"]) if row else 0


def fetch_operations_snapshot(
    conn: sqlite3.Connection, *, now_ts: str
) -> dict[str, Any]:
    """Build the full operational evidence snapshot.

    Consumed by both the dashboard Operations tab and the focused soak gate.
    No side effects; no streamlit imports.
    """
    sources = store.fetch_source_health(conn)
    for item in sources.values():
        item["last_poll_age_seconds"] = _age_seconds(now_ts, item.get("last_poll_ts"))
        item["last_event_age_seconds"] = _age_seconds(now_ts, item.get("last_event_ts"))

    return {
        "sources": sources,
        "llm_calls": fetch_llm_role_summary(conn),
        "deferred_salience": fetch_deferred_summary(conn),
        "queue_lanes": queue_store.lane_depth(conn),
        "delivery_groups": {
            "failed": fetch_failed_delivery_groups(conn),
            "skipped_only": fetch_skipped_only_delivery_group_count(conn),
        },
        "costs": fetch_provider_split(conn),
    }
