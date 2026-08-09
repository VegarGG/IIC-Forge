"""Operator-safe status and alert queries for the dashboard."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

from tradingagents.ops.status import collect_status


def fetch_system_status(
    conn: sqlite3.Connection,
    config: Mapping[str, Any],
    *,
    backup_root: str | Path,
) -> dict[str, Any]:
    return collect_status(
        conn,
        config,
        backup_root=backup_root,
        full_database_check=False,
        redis_check=False,
    )


def fetch_open_alerts(conn: sqlite3.Connection, *, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT alert_id, category, severity, summary, first_seen_ts, last_seen_ts, "
        "occurrence_count, last_delivery_ts FROM operational_alerts "
        "WHERE state='open' ORDER BY severity DESC, last_seen_ts DESC LIMIT ?",
        (max(1, min(limit, 500)),),
    )
    return [dict(row) for row in rows]
