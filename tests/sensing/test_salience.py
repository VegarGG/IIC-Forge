import json
import pytest
import fakeredis.aioredis
from datetime import datetime, timezone

from tradingagents.sensing.envelope import Envelope


def _env(text="Apple beats", source="polygon_news"):
    return Envelope(
        source=source,
        ingested_ts=datetime.now(timezone.utc).isoformat(),
        external_id="x:1", text=text, source_tags={}, raw_path="p",
    )


@pytest.fixture
def llm_factory():
    counter = {"n": 0}
    def factory(prompt: str) -> str:
        counter["n"] += 1
        return json.dumps({
            "salience": 0.85,
            "matched_tickers": ["AAPL"],
            "mentioned_tickers": [{"ticker": "AAPL", "confidence": 0.95}],
            "reason": "beats consensus",
        })
    return factory, counter


@pytest.mark.unit
async def test_salience_first_call_invokes_llm(llm_factory):
    from tradingagents.sensing.salience import SalienceScorer
    factory, counter = llm_factory
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    s = SalienceScorer(redis=r, llm_call=factory, cache_ttl_seconds=86400)
    result = await s.score(env=_env(), watchlist=["AAPL"], macro_context="")
    assert result.salience == pytest.approx(0.85)
    assert counter["n"] == 1


@pytest.mark.unit
async def test_salience_second_call_hits_cache(llm_factory):
    from tradingagents.sensing.salience import SalienceScorer
    factory, counter = llm_factory
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    s = SalienceScorer(redis=r, llm_call=factory, cache_ttl_seconds=86400)
    env = _env(text="Same text")
    await s.score(env=env, watchlist=["AAPL"], macro_context="")
    await s.score(env=env, watchlist=["AAPL"], macro_context="")
    assert counter["n"] == 1


@pytest.mark.unit
async def test_salience_handles_malformed_llm_json():
    from tradingagents.sensing.salience import SalienceScorer
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    s = SalienceScorer(
        redis=r,
        llm_call=lambda _: "not valid json at all",
        cache_ttl_seconds=86400,
    )
    result = await s.score(env=_env(), watchlist=[], macro_context="")
    assert 0.0 <= result.salience <= 0.3
    assert result.mentioned_tickers == []
    # Task 9: failure path now returns source="deferred"; reason carries the
    # error type.  Accept both the old "fallback"/"parse" wording and the new
    # "deferred" wording so the test is forward-compatible.
    assert (
        "fallback" in result.reason.lower()
        or "parse" in result.reason.lower()
        or "deferred" in result.reason.lower()
    )


# ---------------------------------------------------------------------------
# Fix 4: legacy cache blob (out-of-range salience) treated as a cache miss
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_legacy_out_of_range_cache_blob_treated_as_miss(llm_factory):
    """A cached blob with salience outside [0.0, 1.0] (e.g. from a prior
    branch before bounds validation was added) must NOT dead-letter the event.
    score() should fall through to the LLM and return a valid result."""
    import json as _json
    import fakeredis.aioredis

    from tradingagents.sensing.salience import SalienceScorer, _cache_key

    factory, counter = llm_factory
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    env = _env(text="Legacy blob test")

    # Seed Redis with an out-of-range legacy blob directly (bypassing _serialize).
    bad_blob = _json.dumps({
        "salience": 7.5,   # out of range — validator raises ValueError
        "matched_tickers": [],
        "mentioned_tickers": [],
        "reason": "cached pre-branch",
    })
    key = _cache_key(env)
    await r.setex(key, 86400, bad_blob)

    s = SalienceScorer(redis=r, llm_call=factory, cache_ttl_seconds=86400)
    result = await s.score(env=env, watchlist=["AAPL"], macro_context="")

    # Should have fallen through to the LLM (counter bumped).
    assert counter["n"] == 1, (
        "Expected live LLM call after legacy cache miss; got 0 LLM calls"
    )
    # Result is valid (from the stub LLM, not the bad blob).
    assert 0.0 <= result.salience <= 1.0, (
        f"score() returned out-of-range salience {result.salience!r} from bad blob"
    )
    assert result.source == "llm"
