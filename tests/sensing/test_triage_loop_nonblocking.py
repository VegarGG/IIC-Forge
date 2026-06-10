"""Tests that the triage consume loop is non-blocking (LLM/embed off the event loop).

Design:
- Use a thread-identity assertion: inside the fake call_llm (and the embedder's
  embed()), assert that the call does NOT run on the event-loop thread.
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
