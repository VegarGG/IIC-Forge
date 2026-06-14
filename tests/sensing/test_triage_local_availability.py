"""Task 15 (D5) — triage availability policy tests.

Covers:
  - the eager startup probe (GET /health + 1-token completion) against the
    StubOpenAIServer and against a guaranteed-dead endpoint;
  - ``_main`` refusing to start with ``fallback="none"`` and a dead endpoint,
    and starting + routing to the global provider with ``fallback="api"``;
  - runtime degradation: a per-call LLM failure marks the event
    ``salience_source='deferred'`` (NULL salience), skips dedupe RECORDING so a
    redelivery is re-scored, and increments the persistent failure counter;
  - ops_counters store helpers and the daily fallback budget.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path

import fakeredis.aioredis
import pytest

from tests.llm_clients.conftest import StubOpenAIServer
from tradingagents.persistence import store
from tradingagents.persistence.db import connect
from tradingagents.sensing.envelope import Envelope


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _dead_base_url() -> str:
    """Reserve a port via bind-and-close so connections are refused."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1"


def _env(text="Apple reports a big beat on Q3 revenue", source="polygon_news",
         raw_path="data/events/staging/x.json"):
    return Envelope(
        source=source,
        ingested_ts=datetime.now(timezone.utc).isoformat(),
        external_id=f"x:{text[:10]}",
        text=text, source_tags={}, raw_path=raw_path,
    )


@pytest.fixture
def conn(tmp_path):
    c = connect(str(tmp_path / "iic.db"))
    store.upsert_ticker(c, ticker="AAPL", exchange="NASDAQ",
                        name="Apple Inc.", aliases=[], active=True)
    return c


@pytest.fixture
def stub_server():
    s = StubOpenAIServer()
    yield s
    s.shutdown()


def _role_entry(**overrides):
    entry = {
        "provider": None, "model": None, "base_url": None,
        "extra_body": None, "fallback": "none",
        "fallback_threshold": 3, "fallback_daily_budget": 500,
    }
    entry.update(overrides)
    return entry


# ---------------------------------------------------------------------------
# Probe contract
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_probe_sends_health_and_one_token_completion(stub_server):
    from tradingagents.llm_clients.availability import probe_local_endpoint

    probe_local_endpoint(base_url=stub_server.url + "/v1", model="probe-model")

    body = stub_server.last_request_json
    assert body is not None, "probe did not POST a completion"
    assert body["model"] == "probe-model"
    assert body["max_tokens"] == 1


@pytest.mark.unit
def test_probe_passes_without_health_endpoint_via_completion_check(caplog):
    """/health is a llama-server convention — an endpoint that 404s it but
    answers the 1-token completion is ALIVE: warn and pass, don't refuse."""
    from tradingagents.llm_clients.availability import probe_local_endpoint

    caplog.set_level(logging.WARNING)
    s = StubOpenAIServer(serve_health=False)
    try:
        probe_local_endpoint(base_url=s.url + "/v1", model="probe-model")
        body = s.last_request_json
    finally:
        s.shutdown()

    # The completion check ran and proved liveness...
    assert body is not None, "probe did not POST a completion"
    assert body["model"] == "probe-model"
    assert body["max_tokens"] == 1
    # ...and the missing /health was surfaced loudly.
    assert "health" in caplog.text.lower()
    assert "404" in caplog.text


@pytest.mark.unit
def test_probe_raises_typed_error_with_endpoint_and_model():
    from tradingagents.llm_clients.availability import (
        LocalEndpointUnavailable, probe_local_endpoint,
    )

    dead = _dead_base_url()
    with pytest.raises(LocalEndpointUnavailable) as ei:
        probe_local_endpoint(base_url=dead, model="probe-model", timeout=2.0)
    msg = str(ei.value)
    assert dead in msg
    assert "probe-model" in msg


# ---------------------------------------------------------------------------
# Startup: _main refuses to start (fallback="none", dead endpoint)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_main_refuses_start_dead_endpoint_fallback_none(monkeypatch, tmp_path, caplog):
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.llm_clients.availability import LocalEndpointUnavailable

    caplog.set_level(logging.INFO)
    dead = _dead_base_url()

    import tradingagents.sensing.redis_client as redis_mod
    monkeypatch.setattr(redis_mod, "make_redis", lambda url: object())
    import tradingagents.sensing.embeddings as emb_mod

    class _FakeEmbedder:
        def load(self):
            pass

    monkeypatch.setattr(emb_mod, "SentenceTransformerEmbedder",
                        lambda model: _FakeEmbedder())

    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    monkeypatch.setitem(DEFAULT_CONFIG, "iic_db_path", str(tmp_path / "iic.db"))
    monkeypatch.setitem(DEFAULT_CONFIG, "iic_data_dir", str(tmp_path / "data"))
    monkeypatch.setitem(DEFAULT_CONFIG, "llm_roles", {
        "triage_salience": _role_entry(
            provider="local", model="test-local-model",
            base_url=dead, fallback="none"),
        "alert_gate": _role_entry(),
    })

    from tradingagents.sensing.triage import _main
    with pytest.raises(LocalEndpointUnavailable) as ei:
        _main()

    msg = str(ei.value)
    assert dead in msg and "test-local-model" in msg
    # Startup log must show the resolved endpoint + model identity.
    assert "test-local-model" in caplog.text
    assert dead in caplog.text


# ---------------------------------------------------------------------------
# Startup: _main starts and routes to the global provider (fallback="api")
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_main_fallback_api_routes_to_global_provider(
    monkeypatch, tmp_path, caplog, stub_server
):
    from tradingagents.default_config import DEFAULT_CONFIG

    caplog.set_level(logging.INFO)
    dead = _dead_base_url()
    db_path = str(tmp_path / "iic.db")

    import tradingagents.sensing.redis_client as redis_mod
    monkeypatch.setattr(redis_mod, "make_redis", lambda url: object())
    import tradingagents.sensing.embeddings as emb_mod

    class _FakeEmbedder:
        def load(self):
            pass

    monkeypatch.setattr(emb_mod, "SentenceTransformerEmbedder",
                        lambda model: _FakeEmbedder())

    # Capture the llm_call closure instead of running the consume loop.
    captured = {}
    import tradingagents.sensing.triage as triage_mod

    class _FakeTriage:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(triage_mod, "Triage", _FakeTriage)
    monkeypatch.setattr(asyncio, "run", lambda coro: coro.close())

    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    monkeypatch.setitem(DEFAULT_CONFIG, "iic_db_path", db_path)
    monkeypatch.setitem(DEFAULT_CONFIG, "iic_data_dir", str(tmp_path / "data"))
    monkeypatch.setitem(DEFAULT_CONFIG, "llm_provider", "deepseek")
    monkeypatch.setitem(DEFAULT_CONFIG, "quick_think_llm", "deepseek-v4-flash")
    monkeypatch.setitem(DEFAULT_CONFIG, "backend_url", stub_server.url + "/v1")
    monkeypatch.setitem(DEFAULT_CONFIG, "llm_roles", {
        "triage_salience": _role_entry(
            provider="local", model="test-local-model",
            base_url=dead, fallback="api"),
        "alert_gate": _role_entry(),
    })

    from tradingagents.sensing.triage import _main
    _main()  # must NOT raise — fallback="api" starts the daemon

    # The daemon's llm_call must route to the GLOBAL provider (the stub).
    llm_call = captured.get("llm_call")
    assert llm_call is not None, "Triage was not constructed with llm_call"
    out = llm_call("ping")
    assert out == '{"ok": true}'
    body = stub_server.last_request_json
    assert body is not None, "global-provider stub never received the call"
    assert body["model"] == "deepseek-v4-flash"

    # The fallback path is logged and budget-bounded (one call consumed).
    assert "fallback" in caplog.text.lower()
    today = datetime.now(timezone.utc).date().isoformat()
    check = connect(db_path)
    assert store.get_ops_counter(
        check, name=f"triage_fallback_calls:{today}") == 1


# ---------------------------------------------------------------------------
# Budget persistence from a WORKER THREAD (mirrors production exactly):
# SalienceScorer dispatches the sync llm_call via asyncio.to_thread, so
# try_consume's ops_counters read/write runs OFF the main thread. With a
# main-thread-bound sqlite conn that raises sqlite3.ProgrammingError, which
# the budget's `except sqlite3.Error` swallows — the budget silently degrades
# to in-memory and the persisted spend stays 0 (restart-proofing broken).
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_fallback_budget_persists_when_called_from_worker_thread(
    monkeypatch, tmp_path, caplog, stub_server
):
    from tradingagents.default_config import DEFAULT_CONFIG

    caplog.set_level(logging.INFO)
    dead = _dead_base_url()
    db_path = str(tmp_path / "iic.db")

    import tradingagents.sensing.redis_client as redis_mod
    monkeypatch.setattr(redis_mod, "make_redis", lambda url: object())
    import tradingagents.sensing.embeddings as emb_mod

    class _FakeEmbedder:
        def load(self):
            pass

    monkeypatch.setattr(emb_mod, "SentenceTransformerEmbedder",
                        lambda model: _FakeEmbedder())

    captured = {}
    import tradingagents.sensing.triage as triage_mod

    class _FakeTriage:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(triage_mod, "Triage", _FakeTriage)
    monkeypatch.setattr(asyncio, "run", lambda coro: coro.close())

    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    monkeypatch.setitem(DEFAULT_CONFIG, "iic_db_path", db_path)
    monkeypatch.setitem(DEFAULT_CONFIG, "iic_data_dir", str(tmp_path / "data"))
    monkeypatch.setitem(DEFAULT_CONFIG, "llm_provider", "deepseek")
    monkeypatch.setitem(DEFAULT_CONFIG, "quick_think_llm", "deepseek-v4-flash")
    monkeypatch.setitem(DEFAULT_CONFIG, "backend_url", stub_server.url + "/v1")
    monkeypatch.setitem(DEFAULT_CONFIG, "llm_roles", {
        "triage_salience": _role_entry(
            provider="local", model="test-local-model",
            base_url=dead, fallback="api"),
        "alert_gate": _role_entry(),
    })

    from tradingagents.sensing.triage import _main
    _main()  # dead local + fallback="api" → starts with the fallback engaged

    llm_call = captured.get("llm_call")
    assert llm_call is not None, "Triage was not constructed with llm_call"

    # Invoke the budget-consuming call FROM A WORKER THREAD, exactly as
    # SalienceScorer._invoke_llm does via asyncio.to_thread in production.
    result: dict = {}

    def _worker():
        try:
            result["out"] = llm_call("ping")
        except BaseException as e:  # surfaced below for a readable failure
            result["err"] = e

    th = threading.Thread(target=_worker, name="fake-to-thread-worker")
    th.start()
    th.join(timeout=30)
    assert not th.is_alive(), "worker thread hung"
    assert "err" not in result, f"llm_call raised in worker: {result.get('err')}"
    assert result["out"] == '{"ok": true}'

    # The consumed budget must be PERSISTED — read back via a fresh conn.
    today = datetime.now(timezone.utc).date().isoformat()
    check = connect(db_path)
    assert store.get_ops_counter(
        check, name=f"triage_fallback_calls:{today}") == 1


# ---------------------------------------------------------------------------
# Runtime fallback="api": call_llm re-resolves after N consecutive failures
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_runtime_fallback_api_engages_after_threshold(
    monkeypatch, tmp_path, caplog
):
    """With a HEALTHY probe at startup, N consecutive runtime failures
    (counted by process_one via the deferred path) make call_llm re-resolve
    to the global provider, bounded by the daily budget."""
    from tradingagents.default_config import DEFAULT_CONFIG

    caplog.set_level(logging.INFO)
    stub_local = StubOpenAIServer()
    stub_global = StubOpenAIServer()
    db_path = str(tmp_path / "iic.db")
    try:
        import tradingagents.sensing.redis_client as redis_mod
        monkeypatch.setattr(redis_mod, "make_redis", lambda url: object())
        import tradingagents.sensing.embeddings as emb_mod

        class _FakeEmbedder:
            def load(self):
                pass

        monkeypatch.setattr(emb_mod, "SentenceTransformerEmbedder",
                            lambda model: _FakeEmbedder())

        captured = {}
        import tradingagents.sensing.triage as triage_mod

        class _FakeTriage:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(triage_mod, "Triage", _FakeTriage)
        monkeypatch.setattr(asyncio, "run", lambda coro: coro.close())

        monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
        monkeypatch.setitem(DEFAULT_CONFIG, "iic_db_path", db_path)
        monkeypatch.setitem(DEFAULT_CONFIG, "iic_data_dir",
                            str(tmp_path / "data"))
        monkeypatch.setitem(DEFAULT_CONFIG, "llm_provider", "deepseek")
        monkeypatch.setitem(DEFAULT_CONFIG, "quick_think_llm",
                            "deepseek-v4-flash")
        monkeypatch.setitem(DEFAULT_CONFIG, "backend_url",
                            stub_global.url + "/v1")
        monkeypatch.setitem(DEFAULT_CONFIG, "llm_roles", {
            "triage_salience": _role_entry(
                provider="local", model="test-local-model",
                base_url=stub_local.url + "/v1", fallback="api",
                fallback_threshold=2),
            "alert_gate": _role_entry(),
        })

        from tradingagents.sensing.triage import _main
        _main()  # probe OK against the live local stub — starts normally

        llm_call = captured.get("llm_call")
        counter = captured.get("availability_counter")
        assert llm_call is not None and counter is not None

        # Simulate what process_one does on two consecutive deferred scores.
        counter.record_failure(reason="deferred: llm_error: ConnectionError")
        counter.record_failure(reason="deferred: llm_error: ConnectionError")

        out = llm_call("ping")  # threshold crossed → re-resolve to global
        assert out == '{"ok": true}'
        body = stub_global.last_request_json
        assert body is not None, "fallback call never reached the global stub"
        assert body["model"] == "deepseek-v4-flash"
        assert "re-resolved" in caplog.text.lower()

        today = datetime.now(timezone.utc).date().isoformat()
        check = connect(db_path)
        assert store.get_ops_counter(
            check, name=f"triage_fallback_calls:{today}") == 1
    finally:
        stub_local.shutdown()
        stub_global.shutdown()


# ---------------------------------------------------------------------------
# Runtime: deferred marking + dedupe-recording skip + failure counter
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_process_one_deferred_marks_event_skips_dedupe_and_counts(
    conn, tmp_path
):
    from tradingagents.llm_clients.availability import AvailabilityCounter
    from tradingagents.sensing.embeddings import MockEmbedder
    from tradingagents.sensing.triage import Triage

    counter = AvailabilityCounter(name="triage_llm_failures", conn=conn)
    calls = {"n": 0}

    def flaky(_prompt):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("connection refused")
        return json.dumps({
            "salience": 0.9, "matched_tickers": ["AAPL"],
            "mentioned_tickers": [{"ticker": "AAPL", "confidence": 0.95}],
            "reason": "test",
        })

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    t = Triage(conn=conn, redis=r, embedder=MockEmbedder(), llm_call=flaky,
               data_dir=str(tmp_path / "data"), availability_counter=counter)

    # A REAL staging raw file: the deferred path must not consume it, or the
    # redelivery's re-scored event ends up with raw_path="" (no raw text for
    # the secretary's compose).
    staging = tmp_path / "staging" / "x.json"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(
        json.dumps({"text": "Apple reports a big beat on Q3 revenue"}),
        encoding="utf-8",
    )
    env = _env(raw_path=str(staging))
    res1 = await t.process_one(env)

    row = conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (res1.event_id,)
    ).fetchone()
    # Un-scored (NULL), retryable — NOT 0.1, NOT a real score.
    assert row["salience"] is None
    assert row["salience_source"] == "deferred"
    # The deferred row still references readable raw text...
    assert row["raw_path"]
    assert "Q3 revenue" in Path(row["raw_path"]).read_text(encoding="utf-8")
    # ...and the staging file was NOT consumed: the redelivery (below) must
    # be able to canonicalize its own copy of the raw text.
    assert staging.exists()

    # Failure counter incremented, in memory AND persisted.
    assert counter.consecutive == 1
    assert counter.total == 1
    assert store.get_ops_counter(conn, name="triage_llm_failures") == 1

    # Dedupe RECORDING was skipped: no fingerprints, no embedding row.
    assert conn.execute(
        "SELECT COUNT(*) FROM event_fingerprints WHERE event_id = ?",
        (res1.event_id,),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM event_embeddings WHERE event_id = ?",
        (res1.event_id,),
    ).fetchone()[0] == 0

    # Redelivery of the SAME envelope is re-scored, not swallowed as duplicate.
    res2 = await t.process_one(env)
    assert res2.status == "triaged"
    assert res2.deduped_of is None
    row2 = conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (res2.event_id,)
    ).fetchone()
    assert row2["salience"] == pytest.approx(0.9)
    assert row2["salience_source"] == "llm"
    # The re-scored event keeps its raw text: the deferred path above must
    # not have consumed the staging file.
    assert row2["raw_path"]
    assert "Q3 revenue" in Path(row2["raw_path"]).read_text(encoding="utf-8")

    # Success resets the consecutive count; the total is monotonic.
    assert counter.consecutive == 0
    assert counter.total == 1


# ---------------------------------------------------------------------------
# Cache hits must NOT reset the consecutive-failure counter: a cached score
# proves nothing about endpoint health, and frequent cache hits during an
# outage would otherwise delay fallback engagement indefinitely.
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_cache_hit_does_not_reset_consecutive_failures(conn, tmp_path):
    from tradingagents.llm_clients.availability import AvailabilityCounter
    from tradingagents.sensing.embeddings import MockEmbedder
    from tradingagents.sensing.salience import (
        MentionedTicker, SalienceResult, _cache_key, _serialize,
    )
    from tradingagents.sensing.triage import Triage

    counter = AvailabilityCounter(name="triage_llm_failures", conn=conn)

    def dead(_prompt):
        raise AssertionError("a cache hit must not contact the LLM endpoint")

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    env = _env()
    # Pre-seed the salience cache so score.source == "cache".
    await r.set(_cache_key(env), _serialize(SalienceResult(
        salience=0.9, matched_tickers=["AAPL"],
        mentioned_tickers=[MentionedTicker(ticker="AAPL", confidence=0.95)],
        reason="seeded",
    )))

    t = Triage(conn=conn, redis=r, embedder=MockEmbedder(), llm_call=dead,
               data_dir=str(tmp_path / "data"), availability_counter=counter)

    # Mid-outage: two consecutive failures already recorded.
    counter.record_failure(reason="deferred: llm_error: ConnectionError")
    counter.record_failure(reason="deferred: llm_error: ConnectionError")

    res = await t.process_one(env)
    assert res.status == "triaged"
    row = conn.execute(
        "SELECT * FROM events WHERE event_id = ?", (res.event_id,)
    ).fetchone()
    assert row["salience_source"] == "cache"

    # No endpoint contact occurred → the consecutive run is UNCHANGED.
    assert counter.consecutive == 2
    assert counter.total == 2


# ---------------------------------------------------------------------------
# ops_counters store helpers + daily fallback budget
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_ops_counter_bump_and_get(conn):
    assert store.get_ops_counter(conn, name="nope") == 0
    assert store.bump_ops_counter(conn, name="x") == 1
    assert store.bump_ops_counter(conn, name="x") == 2
    assert store.bump_ops_counter(conn, name="x", delta=3) == 5
    assert store.get_ops_counter(conn, name="x") == 5


@pytest.mark.unit
def test_daily_fallback_budget_is_bounded_and_persisted(conn):
    from tradingagents.llm_clients.availability import DailyFallbackBudget

    b = DailyFallbackBudget(name="t_budget", max_per_day=2, conn=conn)
    assert b.try_consume() is True
    assert b.try_consume() is True
    assert b.try_consume() is False    # exhausted — hard bound
    today = datetime.now(timezone.utc).date().isoformat()
    assert store.get_ops_counter(conn, name=f"t_budget:{today}") == 2

    # A fresh instance (process restart) re-reads the persisted spend and
    # stays exhausted — the budget cannot be reset by restarting the daemon.
    b2 = DailyFallbackBudget(name="t_budget", max_per_day=2, conn=conn)
    assert b2.try_consume() is False


@pytest.mark.unit
def test_main_warns_and_refuses_when_fallback_key_missing(
    monkeypatch, tmp_path, caplog
):
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.llm_clients.availability import LocalEndpointUnavailable

    caplog.set_level(logging.INFO)
    dead = _dead_base_url()

    import tradingagents.sensing.redis_client as redis_mod
    monkeypatch.setattr(redis_mod, "make_redis", lambda url: object())
    import tradingagents.sensing.embeddings as emb_mod

    class _FakeEmbedder:
        def load(self):
            pass

    monkeypatch.setattr(emb_mod, "SentenceTransformerEmbedder",
                        lambda model: _FakeEmbedder())

    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    monkeypatch.delenv("IIC_LLM_FALLBACK_API_KEY", raising=False)
    monkeypatch.setitem(DEFAULT_CONFIG, "iic_db_path", str(tmp_path / "iic.db"))
    monkeypatch.setitem(DEFAULT_CONFIG, "iic_data_dir", str(tmp_path / "data"))
    monkeypatch.setitem(DEFAULT_CONFIG, "llm_roles", {
        "triage_salience": _role_entry(
            provider="local", model="test-local-model",
            base_url=dead, fallback="api"),
        "alert_gate": _role_entry(),
    })

    from tradingagents.sensing.triage import _main
    with pytest.raises(LocalEndpointUnavailable):
        _main()

    # Faithful wiring assertion: the guardrail warning must come from triage's
    # OWN logger (distinct from the availability-module resolver log, which also
    # mentions the env var). This proves warn_if_fallback_unsatisfiable was
    # wired into _main with log=log.
    guardrail = [
        r for r in caplog.records
        if r.name == "tradingagents.sensing.triage"
        and "IIC_LLM_FALLBACK_API_KEY" in r.getMessage()
        and r.levelno == logging.WARNING
    ]
    assert guardrail, (
        "expected a guardrail WARNING from the triage logger naming "
        "IIC_LLM_FALLBACK_API_KEY"
    )
    assert any("triage_salience" in r.getMessage() for r in guardrail)
