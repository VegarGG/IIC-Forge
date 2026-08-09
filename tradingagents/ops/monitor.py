"""Persistent operator monitor that turns health conditions into durable alerts."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Mapping

from tradingagents.ops.alerts import record_operational_alert, resolve_absent_alerts
from tradingagents.ops.status import collect_status, health_issues
from tradingagents.persistence.db import connect


log = logging.getLogger(__name__)


def backup_root_from_environment() -> Path:
    return Path(os.environ.get("IIC_BACKUP_DIR", "/backups")).expanduser().resolve()


def run_monitor_cycle(
    config: Mapping[str, Any],
    *,
    backup_root: str | Path,
    require_all_services: bool = True,
) -> dict[str, Any]:
    conn = connect(str(config["iic_db_path"]))
    try:
        snapshot = collect_status(
            conn,
            config,
            backup_root=backup_root,
            full_database_check=False,
            heartbeat_stale_seconds=int(
                config.get("operator_heartbeat_stale_seconds", 90)
            ),
        )
        issues = health_issues(
            snapshot,
            require_all_services=require_all_services,
            max_backup_age_seconds=int(
                config.get("operator_backup_max_age_minutes", 120)
            )
            * 60,
        )
        active: set[str] = set()
        for issue in issues:
            active.add(issue["dedup_key"])
            record_operational_alert(
                conn,
                config=config,
                dedup_key=issue["dedup_key"],
                category=issue["category"],
                severity=issue["severity"],
                summary=issue["summary"],
                details={"monitor_generated_ts": snapshot["generated_ts"]},
            )
        resolved = resolve_absent_alerts(conn, active_dedup_keys=active)
        return {
            "generated_ts": snapshot["generated_ts"],
            "active_issues": len(issues),
            "resolved_alerts": resolved,
        }
    finally:
        conn.close()


def main(config: Mapping[str, Any] | None = None) -> None:
    from tradingagents.default_config import DEFAULT_CONFIG

    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update(config)
    interval = max(10.0, float(cfg.get("operator_monitor_interval_seconds", 30)))
    grace = max(0.0, float(cfg.get("operator_monitor_initial_grace_seconds", 120)))
    backup_root = backup_root_from_environment()
    if grace:
        log.info("operator monitor startup grace", extra={"grace_seconds": grace})
        time.sleep(grace)
    log.info("operator monitor started", extra={"interval_seconds": interval})
    while True:
        try:
            result = run_monitor_cycle(
                cfg, backup_root=backup_root, require_all_services=True
            )
            log.info("operator monitor cycle complete", extra=result)
        except KeyboardInterrupt:
            log.info("operator monitor stopped")
            return
        except Exception:  # noqa: BLE001 - supervisor keeps evaluating
            log.exception("operator monitor cycle failed")
        time.sleep(interval)
