"""Tests that the triage consume loop is non-blocking (LLM/embed off the event loop).

Design:
- Use a thread-identity assertion: inside the fake call_llm AND the embedder's
  embed(), assert that the call does NOT run on the event-loop thread.
- Use a heartbeat counter to smoke-check concurrency: a task increments a counter
  every 10 ms; while a call_llm that sleeps 0.3 s processes one event, the heartbeat
  must still advance (counter >= 1 after the event completes).
- These assertions are fully deterministic — no flaky timing comparisons.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import pytest
import fakeredis.aioredis
from datetime import datetime, timezone

from tradingagents.persistence.db import connect
from tradingagents.persistence.store import upsert_ticker
from tradingagents.sensing.envelope import Envelope
from tradingagents.sensing.redis_client import ensure_consumer_group


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _env(text="Apple Q3 beats expectations"):
    return Envelope(
        source="rss",
        ingested_ts=datetime.now(timezone.utc).isoformat(),
        external_id="x:nonblocking-test",
        text=text,
        source_tags={},
        raw_path="",
    )


@pytest.fixture
def conn(tmp_path):
    conn = connect(str(tmp_path / "iic.db"))
    upsert_ticker(conn, ticker="AAPL", exchange="NASDAQ",
                  name="Apple Inc.", aliases=[], active=True)
    return conn


# ---------------------------------------------------------------------------
# Test 1: LLM call runs OFF the event-loop thread
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_llm_call_runs_off_event_loop_thread():
    """Assert that the sync call_llm closure is executed on a worker thread,
    not on the event-loop thread.
    """
    from tradingagents.sensing.salience import SalienceScorer

    loop_thread = threading.current_thread()
    call_thread_holder: list[threading.Thread] = []

    def sync_llm(prompt: str) -> str:
        # Record which thread we're running on.
        call_thread_holder.append(threading.current_thread())
        return json.dumps({
            "salience": 0.5,
            "matched_tickers": [],
            "mentioned_tickers": [],
            "reason": "test",
        })

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    scorer = SalienceScorer(redis=r, llm_call=sync_llm, cache_ttl_seconds=86400)

    env = _env()
    await scorer.score(env=env, watchlist=[], macro_context="")

    assert call_thread_holder, "call_llm was never invoked"
    worker_thread = call_thread_holder[0]
    assert worker_thread is not loop_thread, (
        "call_llm ran on the event-loop thread — it must run via asyncio.to_thread"
    )


# ---------------------------------------------------------------------------
# Test 2: Heartbeat counter advances while a slow LLM call is in flight
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_event_loop_stays_live_during_slow_llm_call(conn, tmp_path):
    """While call_llm sleeps 0.3 s, a heartbeat task must advance its counter.

    If the event loop blocks, the heartbeat never runs and the counter stays 0.
    """
    from tradingagents.sensing.triage import Triage
    from tradingagents.sensing.embeddings import MockEmbedder

    heartbeat_counter = {"n": 0}

    async def heartbeat():
        """Increment counter every 10 ms so a 300 ms LLM sleep gives >=5 ticks."""
        while True:
            heartbeat_counter["n"] += 1
            await asyncio.sleep(0.01)

    def slow_llm(prompt: str) -> str:
        time.sleep(0.3)  # This would stall the loop if NOT off-thread.
        return json.dumps({
            "salience": 0.5,
            "matched_tickers": [],
            "mentioned_tickers": [],
            "reason": "slow",
        })

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await ensure_consumer_group(r, stream="ingest:raw", group="triage")

    env = _env()
    await r.xadd("ingest:raw", env.to_redis_fields())

    t = Triage(
        conn=conn, redis=r, embedder=MockEmbedder(), llm_call=slow_llm,
        data_dir=str(tmp_path / "data"),
    )

    hb_task = asyncio.create_task(heartbeat())
    # Give the heartbeat a tick to start.
    await asyncio.sleep(0)

    await t.consume_once(
        group="triage", consumer="c0", stream="ingest:raw", block_ms=100, batch=10,
    )

    hb_task.cancel()
    try:
        await hb_task
    except asyncio.CancelledError:
        pass

    assert heartbeat_counter["n"] >= 5, (
        f"Heartbeat advanced only {heartbeat_counter['n']} times during a 300 ms "
        f"LLM call — the event loop was likely blocked. "
        f"Wrap the sync call in asyncio.to_thread."
    )


# ---------------------------------------------------------------------------
# Test 3: Exception inside the to_thread call still routes to deferred
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_llm_exception_via_to_thread_returns_deferred():
    """An exception raised inside the threaded sync call must still produce
    source='deferred' (not crash the scorer or the consume loop).
    """
    from tradingagents.sensing.salience import SalienceScorer

    def exploding_llm(prompt: str) -> str:
        raise RuntimeError("boom from worker thread")

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    scorer = SalienceScorer(redis=r, llm_call=exploding_llm, cache_ttl_seconds=86400)

    result = await scorer.score(env=_env(), watchlist=[], macro_context="")
    assert result.source == "deferred"
    assert "deferred" in result.reason.lower() or "boom" in result.reason.lower()


# ---------------------------------------------------------------------------
# Test 4: Cache hit path does NOT go through to_thread (no LLM call at all)
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_cache_hit_does_not_invoke_llm():
    """When the cache is warm, the LLM should not be called at all."""
    from tradingagents.sensing.salience import SalienceScorer

    call_count = {"n": 0}

    def counting_llm(prompt: str) -> str:
        call_count["n"] += 1
        return json.dumps({
            "salience": 0.7,
            "matched_tickers": [],
            "mentioned_tickers": [],
            "reason": "first",
        })

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    scorer = SalienceScorer(redis=r, llm_call=counting_llm, cache_ttl_seconds=86400)

    env = _env(text="Unique text for cache test")
    # First call populates cache.
    await scorer.score(env=env, watchlist=[], macro_context="")
    # Second call should hit cache — LLM invocation count must stay at 1.
    await scorer.score(env=env, watchlist=[], macro_context="")

    assert call_count["n"] == 1, (
        f"LLM was called {call_count['n']} times; expected 1 (cache should prevent 2nd call)"
    )


# ---------------------------------------------------------------------------
# Test 5: Embedder (stage-2 dedupe) runs OFF the event-loop thread
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_embedder_runs_off_event_loop_thread(conn, tmp_path):
    """Assert that embedder.embed() inside DedupeStage2.check and .record
    is dispatched to a worker thread, not run on the event-loop thread.

    We route an envelope that misses stage-1 (no prior fingerprint) so the
    pipeline must proceed to stage-2, which calls embed().

    The test captures the thread identity of each embed() call and then
    asserts AFTER process_one returns (so the assertion is never swallowed by
    the consume-loop exception handler).
    """
    import fakeredis.aioredis
    from tradingagents.sensing.triage import Triage

    loop_thread = threading.current_thread()
    embed_threads: list[threading.Thread] = []

    class ThreadCheckEmbedder:
        def embed(self, text: str):
            embed_threads.append(threading.current_thread())
            # Return a minimal valid 384-dim zero vector.
            return [0.0] * 384

    def simple_llm(prompt: str) -> str:
        return json.dumps({
            "salience": 0.5,
            "matched_tickers": [],
            "mentioned_tickers": [],
            "reason": "test",
        })

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)

    # Use a unique text so stage-1 fingerprint misses and stage-2 runs.
    env = _env(text="Unique embedder thread check event xyzzy-42")

    t = Triage(
        conn=conn, redis=r,
        embedder=ThreadCheckEmbedder(),
        llm_call=simple_llm,
        data_dir=str(tmp_path / "data"),
    )

    # Call process_one directly so exceptions propagate (not swallowed by
    # the consume-loop _process_entry try/except).
    await t.process_one(env)

    assert embed_threads, (
        "embedder.embed() was never called — check that stage-2 dedupe ran"
    )
    for i, t_thread in enumerate(embed_threads):
        assert t_thread is not loop_thread, (
            f"embedder.embed() call #{i} ran on the event-loop (MainThread) — "
            "it must be dispatched off-thread via asyncio.to_thread or an executor"
        )


# ---------------------------------------------------------------------------
# Tests 6-7: _invoke_llm await-detection — sync callable returning a coroutine
# and object with async __call__
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_sync_callable_returning_coroutine_succeeds():
    """A sync function that returns a coroutine object should still be awaited.

    This catches the regression where iscoroutinefunction() returns False for
    a plain function that returns a coroutine, so the coroutine is passed to
    to_thread un-awaited and the parse step sees a coroutine object instead of
    a str → score source becomes 'deferred' instead of 'llm'.
    """
    import warnings
    from tradingagents.sensing.salience import SalienceScorer

    _RESPONSE = json.dumps({
        "salience": 0.6,
        "matched_tickers": [],
        "mentioned_tickers": [],
        "reason": "awaitable-test",
    })

    async def _inner(prompt: str) -> str:
        return _RESPONSE

    def sync_returns_coroutine(prompt: str):
        # Returns a coroutine object (not a coroutine function itself).
        return _inner(prompt)

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    scorer = SalienceScorer(redis=r, llm_call=sync_returns_coroutine,
                            cache_ttl_seconds=86400)

    env = _env(text="Sync-returns-coroutine test event")
    # Treat unawaited-coroutine RuntimeWarning as an error to ensure none fires.
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        result = await scorer.score(env=env, watchlist=[], macro_context="")

    assert result.source == "llm", (
        f"Expected source='llm' but got source='{result.source}' (reason={result.reason!r}). "
        "A sync callable returning a coroutine must be awaited after to_thread returns it."
    )
    assert abs(result.salience - 0.6) < 1e-9


@pytest.mark.unit
async def test_async_callable_object_succeeds():
    """An object with async __call__ should produce source='llm', not hang or deferred.

    inspect.iscoroutinefunction() returns False for instances whose __call__ is
    defined with 'async def' — such objects take the to_thread branch and are
    rescued by the __await__ fallback.  The result must still be source='llm'.
    """
    import warnings
    from tradingagents.sensing.salience import SalienceScorer

    _RESPONSE = json.dumps({
        "salience": 0.8,
        "matched_tickers": [],
        "mentioned_tickers": [],
        "reason": "async-obj-test",
    })

    class AsyncCallableLLM:
        async def __call__(self, prompt: str) -> str:
            return _RESPONSE

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    scorer = SalienceScorer(redis=r, llm_call=AsyncCallableLLM(),
                            cache_ttl_seconds=86400)

    env = _env(text="Async-callable-object test event")
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        result = await scorer.score(env=env, watchlist=[], macro_context="")

    assert result.source == "llm", (
        f"Expected source='llm' but got source='{result.source}' (reason={result.reason!r}). "
        "An object with async __call__ must be invoked via await, not to_thread."
    )
    assert abs(result.salience - 0.8) < 1e-9


# ---------------------------------------------------------------------------
# Test 8: memory DB guard — Triage must raise ValueError when conn is :memory:
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_triage_rejects_memory_db():
    """Triage must raise ValueError when the conn is an :memory: (or temp) DB.

    _open_ds2_conn calls sqlite3.connect('') for :memory: conns because
    PRAGMA database_list returns file='' — silently opening an anonymous temp
    DB with no schema.  The guard must catch this at construction time.
    """
    import sqlite3
    import fakeredis.aioredis
    from tradingagents.sensing.triage import Triage
    from tradingagents.sensing.embeddings import MockEmbedder

    mem_conn = sqlite3.connect(":memory:")

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with pytest.raises(ValueError, match="file-backed"):
        Triage(
            conn=mem_conn,
            redis=r,
            embedder=MockEmbedder(),
            llm_call=lambda p: "{}",
            data_dir="/tmp/triage-test-memory-guard",
        )


# ---------------------------------------------------------------------------
# Test 9: FK enforcement on ds2 connection — orphan insert must raise
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_ds2_conn_enforces_foreign_keys(tmp_path):
    """The ds2 connection opened by _open_ds2_conn must have FK enforcement on.

    Without PRAGMA foreign_keys=ON an orphan insert into event_embeddings
    (referencing a nonexistent event_id) silently succeeds.  With the pragma
    it must raise IntegrityError.
    """
    import sqlite3
    from tradingagents.persistence.db import connect
    from tradingagents.sensing.triage import _open_ds2_conn

    db_file = str(tmp_path / "iic.db")
    # connect() sets up the schema (including event_embeddings FK constraint).
    main_conn = connect(db_file)

    ds2_conn = _open_ds2_conn(db_file)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            ds2_conn.execute(
                "INSERT INTO event_embeddings (event_id, vec_id, created_ts)"
                " VALUES (?, ?, ?)",
                ("nonexistent-event-id-xyz", 1, "2024-01-01T00:00:00+00:00"),
            )
            ds2_conn.commit()
    finally:
        ds2_conn.close()
        main_conn.close()
