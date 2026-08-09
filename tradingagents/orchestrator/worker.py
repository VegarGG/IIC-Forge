"""Fenced, process-isolated analysis worker.

The parent process owns queue leases and every lifecycle mutation. Each leased
job runs in a fresh child process with its own SQLite connection and LLM
client. A timeout or shutdown terminates that child (and its POSIX process
group) before the parent schedules a retry, so timed-out Python code cannot
continue committing late results in an abandoned thread.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import sqlite3
import sys
import time
import ctypes
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any, Callable, Optional

from tradingagents.orchestrator import queue_store
from tradingagents.orchestrator.dispatch import JobBlockedError, dispatch
from tradingagents.orchestrator.guards import DailyBudgetGuard
from tradingagents.persistence.db import connect


log = logging.getLogger(__name__)


class ChildTerminationError(RuntimeError):
    """The parent could not prove its analysis child stopped."""


def _arm_linux_parent_death_signal(expected_parent_pid: Optional[int] = None) -> None:
    """Make the analysis child die if its worker parent disappears.

    Docker's production target is Linux. ``PR_SET_PDEATHSIG`` closes the
    otherwise-dangerous SIGKILL/OOM window where the parent cannot run its
    normal child cleanup. The parent-PID check closes the race between reading
    the parent and arming the kernel signal.
    """
    if not sys.platform.startswith("linux"):
        return

    parent_pid = expected_parent_pid or os.getppid()
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG = 1
        errno = ctypes.get_errno()
        raise OSError(errno, "could not arm PR_SET_PDEATHSIG")
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)


def boot_sweep(conn: sqlite3.Connection, *, max_age_seconds: int) -> int:
    """Recover leases left behind by a dead parent worker."""
    return queue_store.sweep_stale_leases(conn, max_age_seconds=max_age_seconds)


def _build_secretary(config: dict, conn: sqlite3.Connection):
    """Build all job-scoped runtime clients inside the child process."""
    from tradingagents.llm_clients.factory import create_llm_client
    from tradingagents.secretary.service import Secretary

    client = create_llm_client(
        provider=config["llm_provider"],
        model=config["deep_think_llm"],
        base_url=config.get("backend_url"),
    )
    llm = client.get_llm()
    return Secretary(conn=conn, data_dir=config["iic_data_dir"], llm=llm)


def _record_failure(
    conn: sqlite3.Connection,
    *,
    job: sqlite3.Row | dict[str, Any],
    error: str,
    category: str,
    retryable: bool,
    retry_base_seconds: int,
    retry_cap_seconds: int,
    exit_code: Optional[int] = None,
) -> None:
    try:
        state = queue_store.mark_failure(
            conn,
            job_id=int(job["job_id"]),
            error_msg=error,
            lease_token=str(job["lease_token"]),
            retry_base_seconds=retry_base_seconds,
            retry_cap_seconds=retry_cap_seconds,
            retryable=retryable,
            error_category=category,
            exit_code=exit_code,
        )
    except queue_store.QueueLeaseLost:
        log.warning(
            "job %s failure arrived after its lease was replaced; fenced",
            job["job_id"],
        )
    else:
        log.warning(
            "job %s ended state=%s category=%s attempt=%s/%s",
            job["job_id"],
            state,
            category,
            job["attempt_count"],
            job["max_attempts"],
        )


def drain_one(
    conn: sqlite3.Connection,
    *,
    secretary,
    budget_guard: Optional[DailyBudgetGuard] = None,
    lease_seconds: int = 1500,
    retry_base_seconds: int = 30,
    retry_cap_seconds: int = 900,
) -> bool:
    """Synchronous unit-test seam; production ``main`` uses a child process."""
    if budget_guard is not None and not budget_guard.gate(conn):
        return False

    job = queue_store.lease_one(conn, lease_seconds=lease_seconds)
    if job is None:
        return False

    try:
        result = dispatch(conn, dict(job), secretary=secretary)
        queue_store.mark_done(
            conn,
            job_id=job["job_id"],
            run_ids=result["run_ids"],
            brief_id=result["brief_id"],
            cost_usd=result["cost_usd"],
            lease_token=job["lease_token"],
        )
    except JobBlockedError as exc:
        _record_failure(
            conn,
            job=job,
            error=str(exc),
            category=exc.category,
            retryable=False,
            retry_base_seconds=retry_base_seconds,
            retry_cap_seconds=retry_cap_seconds,
        )
    except queue_store.QueueLeaseLost:
        log.warning("job %d result was fenced", job["job_id"])
    except Exception as exc:  # noqa: BLE001
        _record_failure(
            conn,
            job=job,
            error=f"{type(exc).__name__}: {exc}",
            category="runtime_error",
            retryable=True,
            retry_base_seconds=retry_base_seconds,
            retry_cap_seconds=retry_cap_seconds,
        )
    return True


def _send_child_message(sender: Connection, message: dict[str, Any]) -> None:
    try:
        sender.send(message)
    except (BrokenPipeError, EOFError, OSError):
        # The parent may have timed out and closed its end before exception
        # unwinding completes. Queue reconciliation remains parent-owned.
        pass


def _job_process_entry(
    db_path: str,
    config: dict[str, Any],
    job: dict[str, Any],
    sender: Connection,
) -> None:
    """Child entry point. It never mutates the queue lifecycle row."""
    _arm_linux_parent_death_signal(config.get("_worker_parent_pid"))
    if os.name == "posix":
        try:
            os.setsid()
        except OSError:
            # Parent termination still falls back to the direct child PID.
            pass

    conn: Optional[sqlite3.Connection] = None
    message: dict[str, Any]
    try:
        job_conn = connect(db_path)
        conn = job_conn
        secretary = _build_secretary(config, job_conn)
        result = dispatch(job_conn, job, secretary=secretary)
        message = {"status": "done", "result": result}
    except JobBlockedError as exc:
        message = {
            "status": "blocked",
            "category": exc.category,
            "error": str(exc),
        }
    except BaseException as exc:  # noqa: BLE001
        message = {
            "status": "retry",
            "category": "runtime_error",
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except BaseException as exc:  # noqa: BLE001
                message = {
                    "status": "retry",
                    "category": "database_close",
                    "error": f"{type(exc).__name__}: {exc}",
                }
        # A success is not visible to the parent until the child has closed its
        # SQLite connection. This prevents the parent from acknowledging a job
        # while a child transaction can still commit.
        _send_child_message(sender, message)
        sender.close()


@dataclass(frozen=True)
class ChildOutcome:
    status: str
    category: str
    error: str
    exit_code: Optional[int]
    result: Optional[dict[str, Any]] = None


def _terminate_process(process, *, grace_seconds: float) -> None:
    """Terminate one child and, on POSIX, descendants in its own group."""
    if not process.is_alive():
        process.join(timeout=0)
        return

    group_pid: Optional[int] = None
    pid = process.pid
    if os.name == "posix" and pid is not None:
        try:
            if os.getpgid(pid) == pid:
                group_pid = pid
                os.killpg(pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        except OSError:
            process.terminate()
    else:
        process.terminate()

    process.join(timeout=max(grace_seconds, 0.0))
    if not process.is_alive():
        return

    if group_pid is not None:
        try:
            os.killpg(group_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()
    process.join(timeout=max(grace_seconds, 0.1))
    if process.is_alive():
        raise ChildTerminationError(
            f"analysis child pid={pid} could not be terminated"
        )


def _decode_child_message(message: Any, *, exit_code: Optional[int]) -> ChildOutcome:
    if not isinstance(message, dict):
        return ChildOutcome(
            "retry", "invalid_child_result", "child returned a non-object result",
            exit_code,
        )
    status = message.get("status")
    if status == "done" and isinstance(message.get("result"), dict):
        result = message["result"]
        run_ids = result.get("run_ids")
        brief_id = result.get("brief_id")
        cost_usd = result.get("cost_usd")
        if (
            isinstance(run_ids, list)
            and all(isinstance(run_id, str) for run_id in run_ids)
            and (brief_id is None or isinstance(brief_id, str))
            and isinstance(cost_usd, (int, float))
            and not isinstance(cost_usd, bool)
            and cost_usd >= 0
        ):
            normalized = {
                "run_ids": run_ids,
                "brief_id": brief_id,
                "cost_usd": float(cost_usd),
            }
            return ChildOutcome("done", "", "", exit_code, normalized)
    if status in {"retry", "blocked"}:
        return ChildOutcome(
            str(status),
            str(message.get("category") or "runtime_error")[:120],
            str(message.get("error") or "child failed without detail")[:2000],
            exit_code,
        )
    return ChildOutcome(
        "retry",
        "invalid_child_result",
        "child returned an invalid result envelope",
        exit_code,
    )


def _monitor_process(
    process,
    receiver: Connection,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    terminate_grace_seconds: float,
    shutdown_requested: Callable[[], bool],
) -> ChildOutcome:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if receiver.poll(0):
            try:
                message = receiver.recv()
            except EOFError:
                message = None
            process.join(timeout=terminate_grace_seconds)
            if process.is_alive():
                _terminate_process(process, grace_seconds=terminate_grace_seconds)
            if message is None:
                return ChildOutcome(
                    "retry",
                    "child_exit",
                    f"analysis child closed without a result (exit={process.exitcode})",
                    process.exitcode,
                )
            return _decode_child_message(message, exit_code=process.exitcode)

        if not process.is_alive():
            process.join(timeout=0)
            if receiver.poll(0):
                try:
                    message = receiver.recv()
                except EOFError:
                    pass
                else:
                    return _decode_child_message(
                        message, exit_code=process.exitcode
                    )
            return ChildOutcome(
                "retry",
                "child_exit",
                f"analysis child exited without a result (exit={process.exitcode})",
                process.exitcode,
            )

        if shutdown_requested():
            _terminate_process(process, grace_seconds=terminate_grace_seconds)
            return ChildOutcome(
                "retry",
                "worker_shutdown",
                "analysis child terminated during worker shutdown",
                process.exitcode,
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process(process, grace_seconds=terminate_grace_seconds)
            return ChildOutcome(
                "retry",
                "timeout",
                f"analysis child exceeded {timeout_seconds:g}s wall-clock limit",
                process.exitcode,
            )
        process.join(timeout=min(max(poll_seconds, 0.01), remaining))


def run_one_process(
    conn: sqlite3.Connection,
    *,
    config: dict[str, Any],
    budget_guard: Optional[DailyBudgetGuard] = None,
    process_context=None,
    child_target=None,
    shutdown_requested: Optional[Callable[[], bool]] = None,
) -> bool:
    """Lease one job, execute it in a killable process, and reconcile it."""
    if budget_guard is not None and not budget_guard.gate(conn):
        return False

    timeout_seconds = float(
        config.get("worker_job_timeout_seconds")
        or float(config["worker_job_timeout_min"]) * 60
    )
    if timeout_seconds <= 0:
        raise ValueError("worker job timeout must be positive")
    lease_seconds = int(timeout_seconds) + int(config["queue_lease_margin_seconds"])
    job = queue_store.lease_one(conn, lease_seconds=max(lease_seconds, 1))
    if job is None:
        return False

    retry_base = int(config["queue_retry_base_seconds"])
    retry_cap = int(config["queue_retry_cap_seconds"])
    poll_seconds = float(config.get("worker_process_poll_seconds", 0.5))
    terminate_grace = float(
        config.get("worker_process_terminate_grace_seconds", 5.0)
    )
    shutdown_check = shutdown_requested or (lambda: _shutdown)
    context: Any = process_context or multiprocessing.get_context(
        str(config.get("worker_process_start_method", "spawn"))
    )
    receiver, sender = context.Pipe(duplex=False)
    target = child_target or _job_process_entry
    child_config = dict(config)
    child_config["_worker_parent_pid"] = os.getpid()
    process = context.Process(
        target=target,
        args=(config["iic_db_path"], child_config, dict(job), sender),
        name=f"analysis-job-{job['job_id']}",
    )

    try:
        process.start()
    except Exception as exc:  # noqa: BLE001
        sender.close()
        receiver.close()
        if process.pid is not None:
            _terminate_process(process, grace_seconds=terminate_grace)
            if not process.is_alive():
                process.close()
        _record_failure(
            conn,
            job=job,
            error=f"{type(exc).__name__}: {exc}",
            category="process_start",
            retryable=True,
            retry_base_seconds=retry_base,
            retry_cap_seconds=retry_cap,
        )
        return True

    sender.close()
    try:
        child_pid = process.pid
        if child_pid is None:
            _terminate_process(process, grace_seconds=terminate_grace)
            _record_failure(
                conn,
                job=job,
                error="analysis child started without a process id",
                category="process_start",
                retryable=True,
                retry_base_seconds=retry_base,
                retry_cap_seconds=retry_cap,
            )
            return True
        try:
            queue_store.set_worker_pid(
                conn,
                job_id=job["job_id"],
                lease_token=job["lease_token"],
                worker_pid=child_pid,
            )
        except queue_store.QueueLeaseLost:
            _terminate_process(process, grace_seconds=terminate_grace)
            log.warning("job %d lease was lost before child registration", job["job_id"])
            return True

        outcome = _monitor_process(
            process,
            receiver,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            terminate_grace_seconds=terminate_grace,
            shutdown_requested=shutdown_check,
        )
        if outcome.status == "done":
            assert outcome.result is not None
            try:
                queue_store.mark_done(
                    conn,
                    job_id=job["job_id"],
                    run_ids=outcome.result["run_ids"],
                    brief_id=outcome.result["brief_id"],
                    cost_usd=outcome.result["cost_usd"],
                    lease_token=job["lease_token"],
                    exit_code=outcome.exit_code or 0,
                )
            except queue_store.QueueLeaseLost:
                log.warning("job %d child result was fenced", job["job_id"])
        else:
            _record_failure(
                conn,
                job=job,
                error=outcome.error,
                category=outcome.category,
                retryable=outcome.status != "blocked",
                retry_base_seconds=(
                    0 if outcome.category == "worker_shutdown" else retry_base
                ),
                retry_cap_seconds=retry_cap,
                exit_code=outcome.exit_code,
            )
        return True
    finally:
        receiver.close()
        if not process.is_alive():
            process.close()


_shutdown = False


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        global _shutdown
        _shutdown = True
        log.info("received signal %s; terminating the in-flight child", signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main(
    config: Optional[dict] = None,
    *,
    process_context=None,
    child_target=None,
) -> None:
    from tradingagents.default_config import DEFAULT_CONFIG

    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update(config)

    conn = connect(cfg["iic_db_path"])
    # The supported deployment has exactly one worker. Any running lease found
    # when that worker boots belongs to its dead predecessor and is immediately
    # recoverable; the Linux child is tied to that predecessor by PDEATHSIG.
    swept = boot_sweep(conn, max_age_seconds=0)
    if swept:
        log.warning("boot sweep recovered %d stale lease(s)", swept)

    budget = DailyBudgetGuard(
        enabled=cfg["daily_budget_enabled"],
        daily_usd=cfg["daily_budget_usd"],
    )
    timeout_seconds = float(
        cfg.get("worker_job_timeout_seconds")
        or float(cfg["worker_job_timeout_min"]) * 60
    )
    sweep_max_age = max(timeout_seconds * 2, timeout_seconds + 300)
    sweep_interval = max(float(cfg["worker_poll_interval_s"]), 30.0)
    last_sweep = time.monotonic()

    _install_signal_handlers()
    log.info(
        "worker started: poll=%ss timeout=%ss process_start=%s budget_enabled=%s",
        cfg["worker_poll_interval_s"],
        timeout_seconds,
        cfg.get("worker_process_start_method", "spawn"),
        budget.enabled,
    )

    try:
        while not _shutdown:
            now = time.monotonic()
            if now - last_sweep >= sweep_interval:
                try:
                    recovered = queue_store.sweep_stale_leases(
                        conn,
                        max_age_seconds=int(sweep_max_age),
                        reason="stale_lease_swept_in_loop",
                    )
                    if recovered:
                        log.warning(
                            "in-loop sweep recovered %d stale lease(s)", recovered
                        )
                except Exception:  # noqa: BLE001
                    log.exception("in-loop stale-lease sweep failed")
                last_sweep = now

            try:
                ran = run_one_process(
                    conn,
                    config=cfg,
                    budget_guard=budget,
                    process_context=process_context,
                    child_target=child_target,
                )
            except KeyboardInterrupt:
                break
            except ChildTerminationError:
                # Continuing could let an unfenced child commit after its job
                # is reclaimed. Exit the parent; Linux PDEATHSIG then provides
                # the last-resort direct-child kill.
                log.critical("could not terminate analysis child; exiting worker")
                raise
            except Exception:  # noqa: BLE001
                log.exception("worker loop failure; sleeping 5s and continuing")
                time.sleep(5)
                continue
            if not ran and not _shutdown:
                time.sleep(float(cfg["worker_poll_interval_s"]))
    finally:
        conn.close()

    log.info("worker stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    main()
