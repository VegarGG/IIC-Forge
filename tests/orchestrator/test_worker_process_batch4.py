from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.orchestrator import queue_store
from tradingagents.orchestrator.worker import run_one_process
from tradingagents.persistence import store
from tradingagents.persistence.db import connect


def _child_done(_db_path, _config, _job, sender) -> None:
    sender.send(
        {
            "status": "done",
            "result": {"run_ids": [], "brief_id": None, "cost_usd": 0.0},
        }
    )
    sender.close()


def _child_blocked(_db_path, _config, _job, sender) -> None:
    sender.send(
        {
            "status": "blocked",
            "category": "invalid_payload",
            "error": "operator must repair this payload",
        }
    )
    sender.close()


def _child_hangs(_db_path, config, _job, sender) -> None:
    if os.name == "posix":
        os.setsid()
    Path(config["test_child_pid_path"]).write_text(
        str(os.getpid()), encoding="ascii"
    )
    while True:
        time.sleep(1)


def _child_crashes(_db_path, _config, _job, _sender) -> None:
    os._exit(17)


def _child_returns_invalid_success(_db_path, _config, _job, sender) -> None:
    sender.send(
        {
            "status": "done",
            "result": {"run_ids": "not-a-list", "brief_id": None, "cost_usd": -1},
        }
    )
    sender.close()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _setup_job(tmp_path, *, max_attempts: int = 3):
    db_path = str(tmp_path / "iic.db")
    conn = connect(db_path)
    store.insert_event(
        conn,
        event_id="ev1",
        source="rss",
        ingested_ts="2026-08-08T00:00:00+00:00",
        salience=0.9,
        raw_path=None,
        status="triaged",
        deduped_of=None,
    )
    job_id = queue_store.insert_queue_job(
        conn,
        job_type="event_alert",
        payload=json.dumps({"event_id": "ev1", "ticker": "AAPL"}),
        trigger_event_id="ev1",
        max_attempts=max_attempts,
    )
    config = dict(DEFAULT_CONFIG)
    config.update(
        {
            "iic_db_path": db_path,
            "worker_job_timeout_seconds": 2.0,
            "worker_process_poll_seconds": 0.01,
            "worker_process_terminate_grace_seconds": 0.1,
            "worker_process_start_method": "spawn",
            "queue_lease_margin_seconds": 1,
            "queue_retry_base_seconds": 0,
            "queue_retry_cap_seconds": 0,
        }
    )
    return conn, job_id, config


def _run(conn, config, child_target, *, shutdown_requested=lambda: False):
    return run_one_process(
        conn,
        config=config,
        process_context=multiprocessing.get_context("spawn"),
        child_target=child_target,
        shutdown_requested=shutdown_requested,
    )


class _UnkillableProcess:
    pid = 999_999_999

    def is_alive(self):
        return True

    def join(self, timeout):
        return None

    def terminate(self):
        return None

    def kill(self):
        return None


@pytest.mark.unit
def test_process_worker_parent_commits_success_and_clears_pid(tmp_path):
    conn, job_id, config = _setup_job(tmp_path)

    assert _run(conn, config, _child_done) is True

    row = conn.execute(
        "SELECT state, worker_pid, last_exit_code, error_category "
        "FROM queue_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert dict(row) == {
        "state": "done",
        "worker_pid": None,
        "last_exit_code": 0,
        "error_category": None,
    }


@pytest.mark.unit
def test_unprovable_child_termination_is_fatal():
    from tradingagents.orchestrator.worker import (
        ChildTerminationError,
        _terminate_process,
    )

    with pytest.raises(ChildTerminationError, match="could not be terminated"):
        _terminate_process(_UnkillableProcess(), grace_seconds=0)


@pytest.mark.unit
def test_process_worker_blocks_permanent_failure_until_noted_requeue(tmp_path):
    conn, job_id, config = _setup_job(tmp_path)

    assert _run(conn, config, _child_blocked) is True

    blocked = conn.execute(
        "SELECT state, attempt_count, error_category, blocked_ts, worker_pid "
        "FROM queue_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert blocked["state"] == "blocked"
    assert blocked["attempt_count"] == 1
    assert blocked["error_category"] == "invalid_payload"
    assert blocked["blocked_ts"] is not None
    assert blocked["worker_pid"] is None
    assert queue_store.lease_one(conn) is None

    assert queue_store.retry_error_job(
        conn,
        job_id=job_id,
        operator_note="fixed malformed event payload",
    )
    retried = conn.execute(
        "SELECT state, max_attempts, operator_note, blocked_ts "
        "FROM queue_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert retried["state"] == "queued"
    assert retried["max_attempts"] == 2
    assert retried["operator_note"] == "fixed malformed event payload"
    assert retried["blocked_ts"] is None


@pytest.mark.unit
def test_process_worker_hard_timeout_kills_child_before_retry(tmp_path):
    conn, job_id, config = _setup_job(tmp_path)
    pid_path = tmp_path / "timeout-child.pid"
    config["test_child_pid_path"] = str(pid_path)
    config["worker_job_timeout_seconds"] = 0.25

    started = time.monotonic()
    assert _run(conn, config, _child_hangs) is True
    elapsed = time.monotonic() - started

    child_pid = int(pid_path.read_text(encoding="ascii"))
    assert elapsed < 5
    assert not _pid_exists(child_pid)
    row = conn.execute(
        "SELECT state, error_category, worker_pid, last_exit_code "
        "FROM queue_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row["state"] == "queued"
    assert row["error_category"] == "timeout"
    assert row["worker_pid"] is None
    assert row["last_exit_code"] is not None


@pytest.mark.unit
def test_process_worker_reconciles_child_crash_as_retryable(tmp_path):
    conn, job_id, config = _setup_job(tmp_path)

    assert _run(conn, config, _child_crashes) is True

    row = conn.execute(
        "SELECT state, error_category, worker_pid, last_exit_code "
        "FROM queue_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row["state"] == "queued"
    assert row["error_category"] == "child_exit"
    assert row["worker_pid"] is None
    assert row["last_exit_code"] == 17


@pytest.mark.unit
def test_process_worker_rejects_invalid_child_success_envelope(tmp_path):
    conn, job_id, config = _setup_job(tmp_path)

    assert _run(conn, config, _child_returns_invalid_success) is True

    row = conn.execute(
        "SELECT state, error_category, worker_pid FROM queue_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row["state"] == "queued"
    assert row["error_category"] == "invalid_child_result"
    assert row["worker_pid"] is None


@pytest.mark.unit
def test_process_worker_shutdown_kills_child_and_requeues_immediately(tmp_path):
    conn, job_id, config = _setup_job(tmp_path)
    pid_path = tmp_path / "shutdown-child.pid"
    config["test_child_pid_path"] = str(pid_path)
    config["worker_job_timeout_seconds"] = 30

    assert _run(
        conn,
        config,
        _child_hangs,
        shutdown_requested=pid_path.exists,
    ) is True

    child_pid = int(pid_path.read_text(encoding="ascii"))
    assert not _pid_exists(child_pid)
    row = conn.execute(
        "SELECT state, error_category, available_ts, last_error_ts, worker_pid "
        "FROM queue_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    assert row["state"] == "queued"
    assert row["error_category"] == "worker_shutdown"
    assert row["available_ts"] == row["last_error_ts"]
    assert row["worker_pid"] is None
