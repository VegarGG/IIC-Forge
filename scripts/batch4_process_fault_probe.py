#!/usr/bin/env python3
"""Credential-free field probe for Batch 4 worker process failure handling."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sqlite3
import time
import uuid
from pathlib import Path

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.orchestrator import queue_store
from tradingagents.orchestrator.worker import (
    _arm_linux_parent_death_signal,
    boot_sweep,
    run_one_process,
)
from tradingagents.persistence import store
from tradingagents.persistence.db import connect


def _hanging_child(_db_path, config, _job, _sender) -> None:
    _arm_linux_parent_death_signal(config.get("_worker_parent_pid"))
    if os.name == "posix":
        os.setsid()
    Path(config["probe_pid_path"]).write_text(str(os.getpid()), encoding="ascii")
    while True:
        time.sleep(1)


def _seed(db_path: Path) -> tuple[sqlite3.Connection, int]:
    if db_path.exists():
        raise SystemExit(f"refusing to seed existing probe database: {db_path}")
    conn = connect(str(db_path))
    event_id = f"batch4-{uuid.uuid4().hex}"
    store.insert_event(
        conn,
        event_id=event_id,
        source="batch4-probe",
        ingested_ts="2026-08-08T00:00:00+00:00",
        salience=1.0,
        raw_path=None,
        status="triaged",
        deduped_of=None,
    )
    job_id = queue_store.insert_queue_job(
        conn,
        job_type="event_alert",
        payload=json.dumps({"event_id": event_id, "ticker": "PROBE"}),
        trigger_event_id=event_id,
        idempotency_key=f"batch4-probe:{event_id}",
        max_attempts=2,
    )
    return conn, job_id


def _config(db_path: Path, pid_path: Path, timeout: float) -> dict:
    config = dict(DEFAULT_CONFIG)
    config.update(
        {
            "iic_db_path": str(db_path),
            "probe_pid_path": str(pid_path),
            "worker_job_timeout_seconds": timeout,
            "worker_process_start_method": "spawn",
            "worker_process_poll_seconds": 0.02,
            "worker_process_terminate_grace_seconds": 0.2,
            "queue_lease_margin_seconds": 1,
            "queue_retry_base_seconds": 0,
            "queue_retry_cap_seconds": 0,
        }
    )
    return config


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _timeout_probe(db_path: Path, pid_path: Path) -> None:
    conn, job_id = _seed(db_path)
    run_one_process(
        conn,
        config=_config(db_path, pid_path, 0.5),
        process_context=multiprocessing.get_context("spawn"),
        child_target=_hanging_child,
    )
    child_pid = int(pid_path.read_text(encoding="ascii"))
    row = conn.execute(
        "SELECT state, error_category, worker_pid, last_exit_code "
        "FROM queue_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    result = dict(row)
    result["child_pid"] = child_pid
    result["child_alive"] = _pid_exists(child_pid)
    print(json.dumps(result, sort_keys=True))
    if (
        result["state"] != "queued"
        or result["error_category"] != "timeout"
        or result["worker_pid"] is not None
        or result["child_alive"]
    ):
        raise SystemExit("Batch 4 timeout probe failed")


def _hold_probe(db_path: Path, pid_path: Path) -> None:
    conn, _job_id = _seed(db_path)
    print("probe worker ready; force-kill this container", flush=True)
    run_one_process(
        conn,
        config=_config(db_path, pid_path, 600),
        process_context=multiprocessing.get_context("spawn"),
        child_target=_hanging_child,
    )
    raise SystemExit("hold probe returned without an external kill")


def _recover_probe(db_path: Path) -> None:
    if not db_path.exists():
        raise SystemExit(f"probe database does not exist: {db_path}")
    conn = connect(str(db_path))
    before = conn.execute(
        "SELECT job_id, state, worker_pid FROM queue_jobs ORDER BY job_id DESC LIMIT 1"
    ).fetchone()
    if before is None or before["state"] != "running":
        raise SystemExit("expected one abandoned running job before recovery")
    recovered = boot_sweep(conn, max_age_seconds=0)
    after = conn.execute(
        "SELECT job_id, state, worker_pid, error_category "
        "FROM queue_jobs WHERE job_id = ?",
        (before["job_id"],),
    ).fetchone()
    result = {
        "recovered": recovered,
        "previous_worker_pid": before["worker_pid"],
        **dict(after),
    }
    print(json.dumps(result, sort_keys=True))
    if (
        recovered != 1
        or result["state"] != "queued"
        or result["worker_pid"] is not None
        or result["error_category"] != "lease_expired"
    ):
        raise SystemExit("Batch 4 restart-recovery probe failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("timeout", "hold", "recover"))
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--pid-file", type=Path)
    args = parser.parse_args()
    if args.mode in {"timeout", "hold"} and args.pid_file is None:
        parser.error("--pid-file is required for timeout and hold modes")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    if args.pid_file is not None:
        args.pid_file.parent.mkdir(parents=True, exist_ok=True)
    if args.mode == "timeout":
        _timeout_probe(args.db, args.pid_file)
    elif args.mode == "hold":
        _hold_probe(args.db, args.pid_file)
    else:
        _recover_probe(args.db)


if __name__ == "__main__":
    main()
