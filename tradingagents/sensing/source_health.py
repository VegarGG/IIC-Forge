"""Source health ledger helpers for sensing adapters."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from tradingagents.persistence import store


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_poll_success(
    conn: sqlite3.Connection,
    *,
    source: str,
    service_name: str,
    emitted: int,
    cursor: Optional[str],
    last_event_ts: Optional[str],
    diagnostics: Optional[dict] = None,
) -> None:
    now = now_iso()
    store.upsert_source_health_success(
        conn,
        source=source,
        service_name=service_name,
        last_poll_ts=now,
        last_success_ts=now,
        last_event_ts=last_event_ts,
        cursor=cursor,
        cursor_updated_ts=now if cursor is not None else None,
        events_emitted_last_poll=emitted,
        diagnostics=diagnostics or {},
    )


def record_poll_failure(
    conn: sqlite3.Connection,
    *,
    source: str,
    service_name: str,
    error: BaseException | str,
    diagnostics: Optional[dict] = None,
) -> None:
    store.upsert_source_health_failure(
        conn,
        source=source,
        service_name=service_name,
        last_poll_ts=now_iso(),
        error=str(error),
        diagnostics=diagnostics or {},
    )
