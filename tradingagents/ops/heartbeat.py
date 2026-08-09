"""Durable process heartbeats for independently supervised services."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from tradingagents.ops.logging import redact_fields


log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_detail(detail: dict[str, Any] | None) -> str:
    clean = redact_fields(detail or {})
    return json.dumps(clean, sort_keys=True, separators=(",", ":"))[:4000]


def write_heartbeat(
    db_path: str,
    *,
    service_name: str,
    instance_id: str,
    started_ts: str,
    status: str = "active",
    success: bool = False,
    failure: bool = False,
    detail: dict[str, Any] | None = None,
) -> None:
    """Upsert one heartbeat using a short-lived thread-safe connection."""
    if status not in {"active", "degraded", "stopping"}:
        raise ValueError(f"invalid heartbeat status: {status!r}")
    now = _now()
    conn = sqlite3.connect(db_path, timeout=5)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        with conn:
            conn.execute(
                "INSERT INTO service_heartbeats ("
                "service_name, instance_id, process_id, started_ts, heartbeat_ts, "
                "status, success_ts, failure_ts, failure_count, detail"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(service_name) DO UPDATE SET "
                "instance_id=excluded.instance_id, process_id=excluded.process_id, "
                "started_ts=CASE WHEN service_heartbeats.instance_id = excluded.instance_id "
                "THEN service_heartbeats.started_ts ELSE excluded.started_ts END, "
                "heartbeat_ts=excluded.heartbeat_ts, status=excluded.status, "
                "success_ts=CASE WHEN excluded.success_ts IS NOT NULL "
                "THEN excluded.success_ts ELSE service_heartbeats.success_ts END, "
                "failure_ts=CASE WHEN excluded.failure_ts IS NOT NULL "
                "THEN excluded.failure_ts ELSE service_heartbeats.failure_ts END, "
                "failure_count=CASE WHEN excluded.failure_ts IS NOT NULL "
                "THEN service_heartbeats.failure_count + 1 "
                "WHEN service_heartbeats.instance_id = excluded.instance_id "
                "THEN service_heartbeats.failure_count ELSE 0 END, "
                "detail=excluded.detail",
                (
                    service_name[:120],
                    instance_id,
                    os.getpid(),
                    started_ts,
                    now,
                    status,
                    now if success else None,
                    now if failure else None,
                    1 if failure else 0,
                    _safe_detail(detail),
                ),
            )
    finally:
        conn.close()


class ServiceHeartbeat:
    """Context manager that maintains a durable liveness row."""

    def __init__(
        self,
        db_path: str,
        service_name: str,
        *,
        interval_seconds: float = 15.0,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if interval_seconds < 1:
            raise ValueError("heartbeat interval must be at least one second")
        self.db_path = db_path
        self.service_name = service_name
        self.interval_seconds = interval_seconds
        self.detail = detail or {}
        self.instance_id = uuid.uuid4().hex
        self.started_ts = _now()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _write(self, *, status: str = "active", success: bool = False,
               failure: bool = False, detail: dict[str, Any] | None = None) -> None:
        write_heartbeat(
            self.db_path,
            service_name=self.service_name,
            instance_id=self.instance_id,
            started_ts=self.started_ts,
            status=status,
            success=success,
            failure=failure,
            detail=detail if detail is not None else self.detail,
        )

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self._write()
            except Exception:  # noqa: BLE001 - liveness reporting cannot kill service
                log.exception("failed to persist service heartbeat")

    def start(self) -> "ServiceHeartbeat":
        self._write(success=True)
        self._thread = threading.Thread(
            target=self._run,
            name=f"heartbeat-{self.service_name}",
            daemon=True,
        )
        self._thread.start()
        return self

    def success(self, detail: dict[str, Any] | None = None) -> None:
        self._write(status="active", success=True, detail=detail)

    def failure(self, detail: dict[str, Any] | None = None) -> None:
        self._write(status="degraded", failure=True, detail=detail)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(self.interval_seconds + 1, 5))
        try:
            self._write(status="stopping")
        except Exception:  # noqa: BLE001
            log.exception("failed to mark service heartbeat stopping")

    def __enter__(self) -> "ServiceHeartbeat":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc is not None:
            try:
                self.failure({"exception": type(exc).__name__})
            except Exception:  # noqa: BLE001
                log.exception("failed to mark service heartbeat failure")
        self.stop()
