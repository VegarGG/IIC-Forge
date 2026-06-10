"""Race repro — one shared sqlite conn between AvailabilityCounter and
DailyFallbackBudget, accessed from two threads (production pattern: triage's
event-loop thread calls record_failure, asyncio.to_thread workers call
try_consume).

With each object holding its OWN lock, cross-object conn access is
unserialized: the C-level sqlite3 module raises
``SystemError('error return without exception set')`` (NOT a sqlite3.Error,
so it escapes the persistence except-nets) and persisted counter updates are
lost.  The fix: both objects must share ONE lock when they share one conn —
triage._main passes the same ``threading.Lock`` to both constructors.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from tradingagents.persistence import store
from tradingagents.persistence.db import connect
from tradingagents.sensing.triage import _open_cross_thread_conn


# Modest iteration count: enough to reproduce the race reliably pre-fix
# (hundreds of SystemErrors / lost updates at 2000), fast post-fix (<5s).
_N = 2000


@pytest.mark.unit
def test_shared_conn_shared_lock_serializes_counter_and_budget(tmp_path):
    from tradingagents.llm_clients.availability import (
        AvailabilityCounter, DailyFallbackBudget,
    )

    db_path = str(tmp_path / "iic.db")
    connect(db_path).close()  # apply schema

    # Exactly the triage._main wiring: ONE check_same_thread=False conn shared
    # by both objects, serialized by ONE shared lock.
    shared_conn = _open_cross_thread_conn(db_path)
    # Test-only speedup: skip fsync on the ~4000 commits (the race under test
    # is in the Python/sqlite3 binding layer, not the disk layer).
    shared_conn.execute("PRAGMA synchronous=OFF")
    shared_lock = threading.Lock()
    counter = AvailabilityCounter(
        name="race_failures", conn=shared_conn, lock=shared_lock)
    budget = DailyFallbackBudget(
        name="race_budget", max_per_day=10 * _N, conn=shared_conn,
        lock=shared_lock)

    errors: list[BaseException] = []
    denied = 0
    barrier = threading.Barrier(2)

    def hammer_counter():
        barrier.wait()
        for _ in range(_N):
            try:
                counter.record_failure(reason="race-test")
            except BaseException as e:  # noqa: BLE001 — repro must keep going
                errors.append(e)

    def hammer_budget():
        nonlocal denied
        barrier.wait()
        for _ in range(_N):
            try:
                if not budget.try_consume():
                    denied += 1
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

    t1 = threading.Thread(target=hammer_counter, name="loop-thread")
    t2 = threading.Thread(target=hammer_budget, name="to-thread-worker")
    t1.start(); t2.start()
    t1.join(timeout=60); t2.join(timeout=60)
    assert not t1.is_alive() and not t2.is_alive(), "hammer threads hung"

    # NO exception of any kind may escape record_failure / try_consume —
    # pre-fix this surfaced SystemError('error return without exception set').
    assert errors == [], (
        f"{len(errors)} exceptions escaped; first: {errors[0]!r}")
    assert denied == 0, "budget denied consumes below max_per_day"

    # Persisted values must equal the in-memory counts — pre-fix most
    # persisted bumps were silently lost to interleaved C-level calls.
    assert counter.total == _N
    today = datetime.now(timezone.utc).date().isoformat()
    check = connect(db_path)
    try:
        assert store.get_ops_counter(check, name="race_failures") == _N
        assert store.get_ops_counter(
            check, name=f"race_budget:{today}") == _N
    finally:
        check.close()
