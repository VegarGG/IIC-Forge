"""Tests for SalienceSchema and no-caching-on-failure behavior (Task 9 / D4)."""

import json
import pytest
import fakeredis.aioredis
from datetime import datetime, timezone

from tradingagents.sensing.envelope import Envelope
from tradingagents.sensing.salience import SalienceScorer, SalienceResult, SalienceSchema


def _env(text="Apple beats earnings estimates", source="polygon_news"):
    return Envelope(
        source=source,
        ingested_ts=datetime.now(timezone.utc).isoformat(),
        external_id="x:1",
        text=text,
        source_tags={},
        raw_path="p",
    )


def _raise(prompt: str) -> str:
    raise RuntimeError("simulated LLM failure")


class _TrackingRedis:
    """Wraps fakeredis to count setex calls."""

    def __init__(self):
        self._r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        self.setex_calls = 0

    async def get(self, key):
        return await self._r.get(key)

    async def setex(self, key, ttl, value):
        self.setex_calls += 1
        return await self._r.setex(key, ttl, value)


@pytest.fixture
def fake_redis():
    return _TrackingRedis()


# ---------------------------------------------------------------------------
# Schema-shape test
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_salience_schema_matches_result_fields():
    """SalienceSchema must cover the fields _parse reads."""
    fields = set(SalienceSchema.model_fields)
    assert {"salience", "matched_tickers", "mentioned_tickers", "reason"} <= fields


@pytest.mark.unit
def test_salience_schema_is_valid_json_schema():
    """SalienceSchema.model_json_schema() must produce a valid dict with 'title'."""
    schema = SalienceSchema.model_json_schema()
    assert isinstance(schema, dict)
    assert "title" in schema or "properties" in schema


@pytest.mark.unit
def test_salience_response_format_helper():
    """salience_response_format() returns the json_schema response_format dict with real pins."""
    from tradingagents.sensing.salience import salience_response_format
    fmt = salience_response_format()
    assert fmt["type"] == "json_schema"
    inner = fmt["json_schema"]
    assert inner["name"] == "SalienceResult"
    assert "schema" in inner
    assert inner["schema"]["type"] == "object"
    assert "salience" in inner["schema"]["properties"]


@pytest.mark.unit
def test_salience_response_format_no_bounds_in_schema():
    """salience_response_format() must NOT emit minimum/maximum for salience.

    llama.cpp's GBNF converter has incomplete numeric-bound support; bounds are
    enforced by the field_validator instead of Field(ge=..., le=...).
    """
    from tradingagents.sensing.salience import salience_response_format
    fmt = salience_response_format()
    salience_prop = fmt["json_schema"]["schema"]["properties"]["salience"]
    assert "minimum" not in salience_prop
    assert "maximum" not in salience_prop


# ---------------------------------------------------------------------------
# Failure must NOT write to redis
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_failure_does_not_cache(fake_redis):
    """LLM raises -> result is 'deferred', and NOTHING is written to redis."""
    scorer = SalienceScorer(redis=fake_redis, llm_call=_raise, cache_ttl_seconds=86400)
    result = await scorer.score(env=_env(), watchlist=["NVDA"], macro_context="")
    assert result.source == "deferred"
    assert fake_redis.setex_calls == 0


@pytest.mark.unit
async def test_parse_failure_does_not_cache(fake_redis):
    """Bad JSON from LLM -> result is 'deferred', not cached."""
    scorer = SalienceScorer(
        redis=fake_redis,
        llm_call=lambda _: "not valid json at all",
        cache_ttl_seconds=86400,
    )
    result = await scorer.score(env=_env(), watchlist=[], macro_context="")
    assert result.source == "deferred"
    assert fake_redis.setex_calls == 0


# ---------------------------------------------------------------------------
# Out-of-range salience must be deferred (not cached)
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_out_of_range_salience_high_is_deferred(fake_redis):
    """salience=7.5 from LLM must produce source='deferred' with zero setex calls."""
    bad_payload = json.dumps({"salience": 7.5, "matched_tickers": [], "reason": "bad"})
    scorer = SalienceScorer(
        redis=fake_redis,
        llm_call=lambda _: bad_payload,
        cache_ttl_seconds=86400,
    )
    result = await scorer.score(env=_env(), watchlist=[], macro_context="")
    assert result.source == "deferred"
    assert fake_redis.setex_calls == 0


@pytest.mark.unit
async def test_out_of_range_salience_negative_is_deferred(fake_redis):
    """salience=-0.2 from LLM must produce source='deferred' with zero setex calls."""
    bad_payload = json.dumps({"salience": -0.2, "matched_tickers": [], "reason": "bad"})
    scorer = SalienceScorer(
        redis=fake_redis,
        llm_call=lambda _: bad_payload,
        cache_ttl_seconds=86400,
    )
    result = await scorer.score(env=_env(), watchlist=[], macro_context="")
    assert result.source == "deferred"
    assert fake_redis.setex_calls == 0


# ---------------------------------------------------------------------------
# Success must still write to redis
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_success_still_caches(fake_redis):
    """A successful LLM call must still write to redis."""
    good_payload = json.dumps({
        "salience": 0.85,
        "matched_tickers": ["AAPL"],
        "mentioned_tickers": [{"ticker": "AAPL", "confidence": 0.95}],
        "reason": "beats consensus",
    })
    scorer = SalienceScorer(
        redis=fake_redis,
        llm_call=lambda _: good_payload,
        cache_ttl_seconds=86400,
    )
    result = await scorer.score(env=_env(), watchlist=["AAPL"], macro_context="")
    assert result.source == "llm"
    assert result.salience == pytest.approx(0.85)
    assert fake_redis.setex_calls == 1


# ---------------------------------------------------------------------------
# Fence-tolerant _parse: ```json ... ``` and ```JSON ... ``` wrapping
# ---------------------------------------------------------------------------

@pytest.mark.unit
async def test_fence_tolerant_parse(fake_redis):
    """LLM wrapping valid JSON in ```json...``` fences must be parsed correctly."""
    fenced = '```json\n{"salience": 0.7, "matched_tickers": ["TSLA"], "mentioned_tickers": [], "reason": "relevant"}\n```'
    scorer = SalienceScorer(
        redis=fake_redis,
        llm_call=lambda _: fenced,
        cache_ttl_seconds=86400,
    )
    result = await scorer.score(env=_env(), watchlist=["TSLA"], macro_context="")
    assert result.salience == pytest.approx(0.7)
    assert result.source == "llm"


@pytest.mark.unit
async def test_fence_tolerant_parse_uppercase_json_tag(fake_redis):
    """LLM wrapping valid JSON in ```JSON...``` (uppercase) fences must be parsed correctly."""
    fenced = '```JSON\n{"salience": 0.6, "matched_tickers": [], "mentioned_tickers": [], "reason": "macro"}\n```'
    scorer = SalienceScorer(
        redis=fake_redis,
        llm_call=lambda _: fenced,
        cache_ttl_seconds=86400,
    )
    result = await scorer.score(env=_env(), watchlist=[], macro_context="")
    assert result.salience == pytest.approx(0.6)
    assert result.source == "llm"


@pytest.mark.unit
async def test_deferred_result_has_zero_salience(fake_redis):
    """Deferred results must have a valid (zero/low) salience, not a poison value."""
    scorer = SalienceScorer(redis=fake_redis, llm_call=_raise, cache_ttl_seconds=86400)
    result = await scorer.score(env=_env(), watchlist=[], macro_context="")
    assert 0.0 <= result.salience <= 0.5
    assert result.matched_tickers == []
    assert result.mentioned_tickers == []
