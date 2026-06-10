"""Tests for shadow_eval table and store helpers.

Covers:
- insert_shadow_eval round-trips all column types including NULLs
- Three row shapes: salience-only, verdict-only, both
- fetch_shadow_eval returns rows in insertion order
- fetch_shadow_eval with model_id filter
- fetch_shadow_eval with limit
- connect() on an existing DB is idempotent (schema re-run safe)
"""

import pytest
from datetime import datetime, timezone

from tradingagents.persistence.db import connect
from tradingagents.persistence import store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# insert_shadow_eval / fetch_shadow_eval round-trip
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_insert_shadow_eval_salience_only(tmp_path):
    """Salience-only row: verdict columns are NULL."""
    conn = connect(str(tmp_path / "iic.db"))
    row_id = store.insert_shadow_eval(
        conn,
        event_id="evt-001",
        model_id="local-model-v1",
        api_salience=0.80,
        local_salience=0.75,
        salience_delta=-0.05,
        api_verdict=None,
        local_verdict=None,
        parse_ok=True,
        latency_ms=120,
        created_ts=_now(),
    )
    assert row_id is not None and row_id > 0

    rows = store.fetch_shadow_eval(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["event_id"] == "evt-001"
    assert r["model_id"] == "local-model-v1"
    assert r["api_salience"] == pytest.approx(0.80)
    assert r["local_salience"] == pytest.approx(0.75)
    assert r["salience_delta"] == pytest.approx(-0.05)
    assert r["api_verdict"] is None
    assert r["local_verdict"] is None
    assert r["parse_ok"] == 1           # stored as INTEGER 0/1
    assert r["latency_ms"] == 120
    assert r["created_ts"] is not None


@pytest.mark.unit
def test_insert_shadow_eval_verdict_only(tmp_path):
    """Verdict-only row: salience columns are NULL."""
    conn = connect(str(tmp_path / "iic.db"))
    store.insert_shadow_eval(
        conn,
        event_id="evt-002",
        model_id="local-model-v1",
        api_salience=None,
        local_salience=None,
        salience_delta=None,
        api_verdict="pass",
        local_verdict="pass",
        parse_ok=True,
        latency_ms=88,
        created_ts=_now(),
    )
    rows = store.fetch_shadow_eval(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["api_salience"] is None
    assert r["local_salience"] is None
    assert r["salience_delta"] is None
    assert r["api_verdict"] == "pass"
    assert r["local_verdict"] == "pass"


@pytest.mark.unit
def test_insert_shadow_eval_both_roles(tmp_path):
    """Both salience and verdict columns populated."""
    conn = connect(str(tmp_path / "iic.db"))
    store.insert_shadow_eval(
        conn,
        event_id="evt-003",
        model_id="local-model-v2",
        api_salience=0.92,
        local_salience=0.89,
        salience_delta=-0.03,
        api_verdict="pass",
        local_verdict="reject",
        parse_ok=False,
        latency_ms=210,
        created_ts=_now(),
    )
    rows = store.fetch_shadow_eval(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["api_salience"] == pytest.approx(0.92)
    assert r["local_salience"] == pytest.approx(0.89)
    assert r["api_verdict"] == "pass"
    assert r["local_verdict"] == "reject"
    assert r["parse_ok"] == 0           # False stored as 0


@pytest.mark.unit
def test_shadow_eval_required_columns_present(tmp_path):
    """All required columns are present in returned dicts."""
    conn = connect(str(tmp_path / "iic.db"))
    store.insert_shadow_eval(
        conn,
        event_id="evt-cols",
        model_id="m1",
        api_salience=0.5,
        local_salience=0.5,
        salience_delta=0.0,
        api_verdict="pass",
        local_verdict="pass",
        parse_ok=True,
        latency_ms=50,
        created_ts=_now(),
    )
    rows = store.fetch_shadow_eval(conn)
    assert len(rows) == 1
    r = rows[0]
    required = {
        "event_id", "model_id", "api_salience", "local_salience",
        "salience_delta", "api_verdict", "local_verdict",
        "parse_ok", "latency_ms", "created_ts",
    }
    assert required.issubset(r.keys())


@pytest.mark.unit
def test_fetch_shadow_eval_insertion_order(tmp_path):
    """Rows come back in insertion order (by shadow_id)."""
    conn = connect(str(tmp_path / "iic.db"))
    ts = _now()
    for i in range(3):
        store.insert_shadow_eval(
            conn,
            event_id=f"evt-ord-{i}",
            model_id="m1",
            api_salience=0.1 * (i + 1),
            local_salience=None,
            salience_delta=None,
            api_verdict=None,
            local_verdict=None,
            parse_ok=True,
            latency_ms=10 * (i + 1),
            created_ts=ts,
        )
    rows = store.fetch_shadow_eval(conn)
    assert len(rows) == 3
    assert [r["event_id"] for r in rows] == ["evt-ord-0", "evt-ord-1", "evt-ord-2"]


@pytest.mark.unit
def test_fetch_shadow_eval_model_id_filter(tmp_path):
    """model_id filter returns only matching rows."""
    conn = connect(str(tmp_path / "iic.db"))
    ts = _now()
    store.insert_shadow_eval(
        conn, event_id="e1", model_id="model-A",
        api_salience=0.7, local_salience=0.6, salience_delta=-0.1,
        api_verdict=None, local_verdict=None,
        parse_ok=True, latency_ms=100, created_ts=ts,
    )
    store.insert_shadow_eval(
        conn, event_id="e2", model_id="model-B",
        api_salience=None, local_salience=None, salience_delta=None,
        api_verdict="pass", local_verdict="pass",
        parse_ok=True, latency_ms=200, created_ts=ts,
    )
    store.insert_shadow_eval(
        conn, event_id="e3", model_id="model-A",
        api_salience=0.5, local_salience=0.4, salience_delta=-0.1,
        api_verdict="reject", local_verdict="reject",
        parse_ok=True, latency_ms=150, created_ts=ts,
    )

    rows_a = store.fetch_shadow_eval(conn, model_id="model-A")
    assert len(rows_a) == 2
    assert all(r["model_id"] == "model-A" for r in rows_a)

    rows_b = store.fetch_shadow_eval(conn, model_id="model-B")
    assert len(rows_b) == 1
    assert rows_b[0]["event_id"] == "e2"

    rows_none = store.fetch_shadow_eval(conn, model_id="nonexistent")
    assert rows_none == []


@pytest.mark.unit
def test_fetch_shadow_eval_limit(tmp_path):
    """limit parameter caps the number of returned rows."""
    conn = connect(str(tmp_path / "iic.db"))
    ts = _now()
    for i in range(5):
        store.insert_shadow_eval(
            conn, event_id=f"e-lim-{i}", model_id="m1",
            api_salience=0.1, local_salience=None, salience_delta=None,
            api_verdict=None, local_verdict=None,
            parse_ok=True, latency_ms=10, created_ts=ts,
        )
    rows = store.fetch_shadow_eval(conn, limit=3)
    assert len(rows) == 3


@pytest.mark.unit
def test_fetch_shadow_eval_model_id_and_limit(tmp_path):
    """model_id filter + limit work together."""
    conn = connect(str(tmp_path / "iic.db"))
    ts = _now()
    for i in range(4):
        store.insert_shadow_eval(
            conn, event_id=f"e-combo-{i}", model_id="m1",
            api_salience=0.5, local_salience=None, salience_delta=None,
            api_verdict=None, local_verdict=None,
            parse_ok=True, latency_ms=10, created_ts=ts,
        )
    # one different model to ensure filter is applied before limit
    store.insert_shadow_eval(
        conn, event_id="e-other", model_id="m2",
        api_salience=None, local_salience=None, salience_delta=None,
        api_verdict="pass", local_verdict="pass",
        parse_ok=True, latency_ms=10, created_ts=ts,
    )
    rows = store.fetch_shadow_eval(conn, model_id="m1", limit=2)
    assert len(rows) == 2
    assert all(r["model_id"] == "m1" for r in rows)


@pytest.mark.unit
def test_connect_idempotent_schema_rerun(tmp_path):
    """connect() on an existing DB must not raise (schema is re-run safely)."""
    db_path = str(tmp_path / "iic.db")
    conn1 = connect(db_path)
    store.insert_shadow_eval(
        conn1, event_id="e-idem", model_id="m1",
        api_salience=0.9, local_salience=0.8, salience_delta=-0.1,
        api_verdict="pass", local_verdict="pass",
        parse_ok=True, latency_ms=55, created_ts=_now(),
    )
    # Second connect on same path — must not raise.
    conn2 = connect(db_path)
    rows = store.fetch_shadow_eval(conn2)
    assert len(rows) == 1


@pytest.mark.unit
def test_shadow_eval_event_id_no_fk_constraint(tmp_path):
    """event_id is a plain TEXT column with no FK — shadow rows survive
    without a matching events row (immune to event lifecycle)."""
    conn = connect(str(tmp_path / "iic.db"))
    # Insert shadow_eval referencing a non-existent event — must not raise.
    store.insert_shadow_eval(
        conn,
        event_id="dangling-event-id",
        model_id="m1",
        api_salience=0.6,
        local_salience=0.5,
        salience_delta=-0.1,
        api_verdict=None,
        local_verdict=None,
        parse_ok=True,
        latency_ms=77,
        created_ts=_now(),
    )
    rows = store.fetch_shadow_eval(conn)
    assert len(rows) == 1
    assert rows[0]["event_id"] == "dangling-event-id"


# ---------------------------------------------------------------------------
# fetch_shadow_eval newest=True — Issue 3
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_fetch_shadow_eval_newest_limit(tmp_path):
    """newest=True + limit returns the NEWEST N rows, re-sorted ascending by
    shadow_id so callers get a consistent time-ordered slice."""
    conn = connect(str(tmp_path / "iic.db"))
    ts = _now()
    # Insert 5 rows: event ids old-0..old-4
    for i in range(5):
        store.insert_shadow_eval(
            conn, event_id=f"old-{i}", model_id="m1",
            api_salience=0.1, local_salience=None, salience_delta=None,
            api_verdict=None, local_verdict=None,
            parse_ok=True, latency_ms=10, created_ts=ts,
        )
    # 2 newest rows: new-0, new-1
    for i in range(2):
        store.insert_shadow_eval(
            conn, event_id=f"new-{i}", model_id="m1",
            api_salience=0.9, local_salience=None, salience_delta=None,
            api_verdict=None, local_verdict=None,
            parse_ok=True, latency_ms=999, created_ts=ts,
        )

    rows = store.fetch_shadow_eval(conn, limit=2, newest=True)
    assert len(rows) == 2
    # Must be re-sorted ascending (shadow_id order), but contain the 2 newest.
    assert all(r["latency_ms"] == 999 for r in rows)
    # Ascending shadow_id order preserved.
    assert rows[0]["shadow_id"] < rows[1]["shadow_id"]


@pytest.mark.unit
def test_fetch_shadow_eval_newest_with_model_id(tmp_path):
    """newest=True + model_id filter: returns newest N rows for that model."""
    conn = connect(str(tmp_path / "iic.db"))
    ts = _now()
    # 3 rows for model-A (old), 2 rows for model-B, 2 rows for model-A (new)
    for i in range(3):
        store.insert_shadow_eval(
            conn, event_id=f"a-old-{i}", model_id="model-A",
            api_salience=0.1, local_salience=None, salience_delta=None,
            api_verdict=None, local_verdict=None,
            parse_ok=True, latency_ms=10, created_ts=ts,
        )
    for i in range(2):
        store.insert_shadow_eval(
            conn, event_id=f"b-{i}", model_id="model-B",
            api_salience=None, local_salience=None, salience_delta=None,
            api_verdict="pass", local_verdict="pass",
            parse_ok=True, latency_ms=50, created_ts=ts,
        )
    for i in range(2):
        store.insert_shadow_eval(
            conn, event_id=f"a-new-{i}", model_id="model-A",
            api_salience=0.9, local_salience=None, salience_delta=None,
            api_verdict=None, local_verdict=None,
            parse_ok=True, latency_ms=999, created_ts=ts,
        )

    rows = store.fetch_shadow_eval(conn, model_id="model-A", limit=2, newest=True)
    assert len(rows) == 2
    assert all(r["model_id"] == "model-A" for r in rows)
    assert all(r["latency_ms"] == 999 for r in rows)


@pytest.mark.unit
def test_fetch_shadow_eval_newest_false_unchanged(tmp_path):
    """newest=False (default) still returns oldest N rows."""
    conn = connect(str(tmp_path / "iic.db"))
    ts = _now()
    for i in range(4):
        store.insert_shadow_eval(
            conn, event_id=f"e-{i}", model_id="m1",
            api_salience=0.1, local_salience=None, salience_delta=None,
            api_verdict=None, local_verdict=None,
            parse_ok=True, latency_ms=i * 100, created_ts=ts,
        )
    rows = store.fetch_shadow_eval(conn, limit=2, newest=False)
    assert len(rows) == 2
    # Oldest two: latency_ms 0 and 100
    assert rows[0]["latency_ms"] == 0
    assert rows[1]["latency_ms"] == 100
