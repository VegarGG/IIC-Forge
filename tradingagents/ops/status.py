"""Machine-readable production status without payload or credential exposure."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tradingagents.llm_clients.daily_budget import (
    beijing_budget_date,
    daily_budget_total,
)
from tradingagents.persistence.db import _load_migrations


EXPECTED_SERVICES = (
    "scheduler",
    "rss",
    "telegram-ingest",
    "polygon",
    "triage",
    "promoter",
    "analysis-worker",
    "delivery-worker",
    "telegram-bot",
    "action-handler",
    "operator-monitor",
    "dashboard",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _age_seconds(value: str | None, now: datetime) -> float | None:
    parsed = _parse_time(value)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds())


def _state_counts(conn: sqlite3.Connection, table: str) -> dict[str, int]:
    return {
        str(row["state"]): int(row["n"])
        for row in conn.execute(
            f'SELECT state, COUNT(*) AS n FROM "{table}" GROUP BY state'
        )
    }


def _oldest_age(
    conn: sqlite3.Connection,
    *,
    table: str,
    timestamp_column: str,
    states: tuple[str, ...],
    now: datetime,
) -> float | None:
    placeholders = ",".join("?" for _ in states)
    row = conn.execute(
        f'SELECT MIN("{timestamp_column}") AS oldest FROM "{table}" '
        f"WHERE state IN ({placeholders})",
        states,
    ).fetchone()
    return _age_seconds(row["oldest"] if row else None, now)


def database_status(conn: sqlite3.Connection, *, full: bool = False) -> dict[str, Any]:
    expected = _load_migrations()[-1]
    latest = conn.execute(
        "SELECT version, name, checksum FROM schema_migrations "
        "ORDER BY version DESC LIMIT 1"
    ).fetchone()
    status: dict[str, Any] = {
        "status": "ok",
        "expected_migration": [expected.version, expected.name],
        "migration": [int(latest[0]), str(latest[1])] if latest else None,
    }
    if latest is None or (
        int(latest[0]), str(latest[1]), str(latest[2])
    ) != (expected.version, expected.name, expected.checksum):
        status["status"] = "error"
        status["error"] = "migration_mismatch"
    if full:
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        foreign_keys = len(list(conn.execute("PRAGMA foreign_key_check")))
        status["integrity"] = integrity
        status["foreign_key_violations"] = foreign_keys
        if integrity != ["ok"] or foreign_keys:
            status["status"] = "error"
            status["error"] = "integrity_failure"
    return status


def queue_status(conn: sqlite3.Connection, *, now: datetime) -> dict[str, Any]:
    return {
        "analysis": {
            "counts": _state_counts(conn, "queue_jobs"),
            "oldest_pending_seconds": _oldest_age(
                conn,
                table="queue_jobs",
                timestamp_column="enqueued_ts",
                states=("queued", "running"),
                now=now,
            ),
            "retrying": int(
                conn.execute(
                    "SELECT COUNT(*) FROM queue_jobs "
                    "WHERE state='queued' AND attempt_count > 0"
                ).fetchone()[0]
            ),
        },
        "delivery": {
            "counts": _state_counts(conn, "delivery_queue"),
            "oldest_pending_seconds": _oldest_age(
                conn,
                table="delivery_queue",
                timestamp_column="created_ts",
                states=("queued", "running"),
                now=now,
            ),
            "retrying": int(
                conn.execute(
                    "SELECT COUNT(*) FROM delivery_queue "
                    "WHERE state='queued' AND attempt_count > 0"
                ).fetchone()[0]
            ),
        },
    }


def heartbeat_status(
    conn: sqlite3.Connection, *, now: datetime, stale_seconds: int
) -> dict[str, Any]:
    rows = {
        str(row["service_name"]): row
        for row in conn.execute(
            "SELECT service_name, instance_id, process_id, started_ts, heartbeat_ts, "
            "status, success_ts, failure_ts, failure_count, detail "
            "FROM service_heartbeats ORDER BY service_name"
        )
    }
    services: dict[str, Any] = {}
    for name in EXPECTED_SERVICES:
        row = rows.get(name)
        if row is None:
            services[name] = {"status": "missing", "heartbeat_age_seconds": None}
            continue
        age = _age_seconds(str(row["heartbeat_ts"]), now)
        state = "stale" if age is None or age > stale_seconds else str(row["status"])
        services[name] = {
            "status": state,
            "heartbeat_age_seconds": age,
            "started_ts": row["started_ts"],
            "success_ts": row["success_ts"],
            "failure_ts": row["failure_ts"],
            "failure_count": int(row["failure_count"]),
            "process_id": row["process_id"],
            "instance_id": str(row["instance_id"])[:12],
        }
    return services


def connector_status(conn: sqlite3.Connection, *, now: datetime) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for source, service in (
        ("rss", "rss"),
        ("telegram", "telegram-ingest"),
        ("polygon_news", "polygon"),
    ):
        cursor = conn.execute(
            "SELECT updated_ts FROM ingest_cursor WHERE source=?", (source,)
        ).fetchone()
        event = conn.execute(
            "SELECT MAX(ingested_ts) AS ingested_ts FROM events WHERE source=?",
            (source,),
        ).fetchone()
        quarantine = conn.execute(
            "SELECT MAX(observed_ts) AS observed_ts FROM ingest_quarantine WHERE source=?",
            (source,),
        ).fetchone()
        result[source] = {
            "service": service,
            "cursor_updated_ts": cursor["updated_ts"] if cursor else None,
            "cursor_age_seconds": _age_seconds(
                cursor["updated_ts"] if cursor else None, now
            ),
            "last_successful_ingestion_ts": event["ingested_ts"] if event else None,
            "last_successful_ingestion_age_seconds": _age_seconds(
                event["ingested_ts"] if event else None, now
            ),
            "last_quarantine_ts": quarantine["observed_ts"] if quarantine else None,
        }
    return result


def disk_status(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    target = resolved if resolved.exists() else resolved.parent
    usage = shutil.disk_usage(target)
    return {
        "path": str(resolved),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "free_percent": round((usage.free / usage.total) * 100, 2),
    }


def backup_status(output_root: str | Path, *, now: datetime) -> dict[str, Any]:
    root = Path(output_root).expanduser().resolve()
    marker = root / "latest.json"
    if not marker.is_file():
        return {"status": "missing", "root": str(root)}
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        archive_name = str(data["archive"])
        created = str(data["created_utc"])
        safe_name = Path(archive_name).name == archive_name and archive_name.endswith(
            ".iicbak"
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {"status": "invalid", "root": str(root)}
    archive = root / archive_name
    return {
        "status": "present" if safe_name and archive.is_file() else "invalid",
        "root": str(root),
        "archive": archive_name if safe_name else None,
        "created_utc": created,
        "age_seconds": _age_seconds(created, now),
        "cipher_sha256": str(data.get("cipher_sha256") or "")[:64],
    }


def budget_status(conn: sqlite3.Connection, config: Mapping[str, Any]) -> dict[str, Any]:
    date = beijing_budget_date(timezone_name=str(config["daily_budget_timezone"]))
    total = daily_budget_total(conn, budget_date=date)
    limit = float(config["daily_budget_usd"])
    releases = conn.execute(
        "SELECT COALESCE(SUM(r.released_usd), 0.0) FROM llm_budget_releases r "
        "JOIN llm_budget_ledger l ON l.call_id=r.call_id WHERE l.budget_date=?",
        (date,),
    ).fetchone()[0]
    return {
        "beijing_date": date,
        "timezone": str(config["daily_budget_timezone"]),
        "charged_or_reserved_usd": round(total, 6),
        "limit_usd": limit,
        "remaining_usd": round(max(0.0, limit - total), 6),
        "released_usd": round(float(releases or 0.0), 6),
        "utilization_percent": round((total / limit) * 100, 2) if limit else 100.0,
    }


def recovery_status(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT archive_name, archive_sha256, started_ts, finished_ts, "
        "elapsed_seconds, status, operator_note FROM recovery_drills "
        "ORDER BY drill_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {"status": "never"}
    from tradingagents.ops.logging import redact

    result = dict(row)
    result["operator_note"] = redact(result["operator_note"])
    return result


def collect_status(
    conn: sqlite3.Connection,
    config: Mapping[str, Any],
    *,
    backup_root: str | Path,
    full_database_check: bool = False,
    heartbeat_stale_seconds: int = 90,
    redis_check: bool = True,
) -> dict[str, Any]:
    """Collect bounded operational metadata; never select payload/body fields."""
    now = _now()
    result: dict[str, Any] = {
        "generated_ts": now.isoformat(),
        "database": database_status(conn, full=full_database_check),
        "queues": queue_status(conn, now=now),
        "heartbeats": heartbeat_status(
            conn, now=now, stale_seconds=heartbeat_stale_seconds
        ),
        "connectors": connector_status(conn, now=now),
        "disk": {
            "data": disk_status(str(config["iic_data_dir"])),
            "backup": disk_status(backup_root),
        },
        "backup": backup_status(backup_root, now=now),
        "budget": budget_status(conn, config),
        "recovery_drill": recovery_status(conn),
    }
    if redis_check:
        try:
            from tradingagents.runtime.operations import check_redis

            result["redis"] = check_redis(config)
        except Exception as exc:  # noqa: BLE001
            result["redis"] = {"status": "error", "error": type(exc).__name__}
    return result


def health_issues(
    status: Mapping[str, Any],
    *,
    require_all_services: bool = True,
    max_backup_age_seconds: int = 7200,
) -> list[dict[str, str]]:
    """Convert a status snapshot into stable, deduplicable conditions."""
    issues: list[dict[str, str]] = []

    def add(key: str, category: str, severity: str, summary: str) -> None:
        issues.append(
            {"dedup_key": key, "category": category, "severity": severity,
             "summary": summary}
        )

    database = status.get("database") or {}
    if database.get("status") != "ok":
        add("database:health", "database", "critical", "SQLite health check failed")
    redis = status.get("redis") or {}
    if redis and redis.get("status") != "ok":
        add("redis:health", "redis", "critical", "Redis health check failed")

    heartbeats = status.get("heartbeats") or {}
    for name, service in heartbeats.items():
        state = service.get("status")
        if state == "stale" or (require_all_services and state == "missing"):
            add(
                f"service:{name}:{state}",
                "service_liveness",
                "critical",
                f"Service {name} heartbeat is {state}",
            )
        elif state == "degraded":
            add(
                f"service:{name}:degraded", "service_liveness", "warning",
                f"Service {name} reports degraded health",
            )

    queues = status.get("queues") or {}
    analysis_counts = (queues.get("analysis") or {}).get("counts") or {}
    delivery_counts = (queues.get("delivery") or {}).get("counts") or {}
    for state in ("error", "blocked"):
        if int(analysis_counts.get(state, 0)):
            add(
                f"analysis-queue:{state}", "analysis_queue", "critical",
                f"Analysis queue contains {state} jobs",
            )
    for state in ("dead", "blocked"):
        if int(delivery_counts.get(state, 0)):
            add(
                f"delivery-queue:{state}", "delivery_queue", "critical",
                f"Delivery queue contains {state} jobs",
            )

    for name, disk in (status.get("disk") or {}).items():
        if float(disk.get("free_percent", 100)) < 10 or int(
            disk.get("free_bytes", 2**63)
        ) < 1024**3:
            add(
                f"disk:{name}:low", "disk", "critical",
                f"{name.capitalize()} storage is low",
            )

    backup = status.get("backup") or {}
    if backup.get("status") != "present":
        add("backup:missing", "backup", "critical", "Verified backup marker is unavailable")
    elif backup.get("age_seconds") is None or float(
        backup["age_seconds"]
    ) > max_backup_age_seconds:
        add("backup:stale", "backup", "critical", "Latest verified backup is stale")

    budget = status.get("budget") or {}
    utilization = float(budget.get("utilization_percent", 0))
    if utilization >= 100:
        add("budget:exhausted", "llm_budget", "critical", "Daily LLM budget is exhausted")
    elif utilization >= 90:
        add("budget:near-limit", "llm_budget", "warning", "Daily LLM budget is above 90 percent")
    return issues
