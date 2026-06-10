"""Salience scoring: cheap-LLM call per event with Redis caching.

The cache key is ``salience:<source>:<sha256(text)[:32]>`` so identical text
across sources still hits separately (different prompts), but re-deliveries
of the exact same source+text envelope are free.

LLM responses are parsed strictly via Pydantic; malformed or out-of-range
responses return a deferred sentinel that is NOT cached, so a flaky model
never stalls the pipeline or poisons the cache.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

import redis.asyncio as aioredis
from pydantic import BaseModel, field_validator

from .envelope import Envelope
from .prompts import build_salience_prompt


@dataclass
class MentionedTicker:
    ticker: str
    confidence: float


@dataclass
class SalienceResult:
    salience: float
    matched_tickers: List[str] = field(default_factory=list)
    mentioned_tickers: List[MentionedTicker] = field(default_factory=list)
    reason: str = ""
    source: str = "llm"  # "llm" | "cache" | "deferred"


class _MentionedTickerSchema(BaseModel):
    ticker: str
    confidence: float


class SalienceSchema(BaseModel):
    """Pydantic schema mirroring the parseable fields of SalienceResult.

    Use ``salience_response_format()`` to build the ``response_format`` dict
    for json_schema-mode LLM calls (Task 14 wiring point).
    """
    salience: float
    matched_tickers: List[str] = []
    mentioned_tickers: List[_MentionedTickerSchema] = []
    reason: str = ""

    # Bounds are enforced here via a field_validator rather than
    # Field(ge=0.0, le=1.0) because the latter injects ``minimum``/``maximum``
    # into model_json_schema() output.  llama.cpp's GBNF converter has
    # incomplete numeric-bound support and chokes on those keys.
    @field_validator("salience")
    @classmethod
    def salience_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"salience must be in [0.0, 1.0], got {v!r}")
        return v


def salience_response_format() -> Dict[str, Any]:
    """Return a ``response_format`` dict for json_schema-mode LLM calls.

    Usage (call-site, e.g. Task 14 harness)::

        fmt = salience_response_format()
        response = llm.invoke(prompt, response_format=fmt)

    The scorer itself does not attach this to its ``llm_call`` closure today
    because the closure is a plain ``lambda prompt: llm.invoke(prompt)``
    (see triage._main).  Wiring response_format into the actual request is
    deferred to Task 14 which can update the closure signature.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "SalienceResult",
            "schema": SalienceSchema.model_json_schema(),
            "strict": False,
        },
    }


def _cache_key(env: Envelope) -> str:
    h = hashlib.sha256(env.text.encode("utf-8")).hexdigest()[:32]
    return f"salience:{env.source}:{h}"


# re.IGNORECASE so ```JSON (uppercase) fences are handled the same as ```json.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _strip_fences(blob: str) -> str:
    """Strip ```json ... ``` or ``` ... ``` fences if present (API path tolerance)."""
    m = _FENCE_RE.search(blob)
    if m:
        return m.group(1)
    return blob


def _parse(blob: str) -> SalienceResult:
    # Strip markdown code fences before parsing (API responses may wrap JSON).
    cleaned = _strip_fences(blob.strip())
    data = json.loads(cleaned)
    # Validate via Pydantic for strictness on the local-grammar path.
    # ValidationError (including out-of-range salience) propagates to the
    # caller which returns a deferred sentinel without caching.
    validated = SalienceSchema.model_validate(data)
    return SalienceResult(
        salience=validated.salience,
        matched_tickers=list(validated.matched_tickers),
        mentioned_tickers=[
            MentionedTicker(ticker=m.ticker, confidence=m.confidence)
            for m in validated.mentioned_tickers
        ],
        reason=validated.reason,
    )


def _serialize(r: SalienceResult) -> str:
    return json.dumps({
        "salience": r.salience,
        "matched_tickers": r.matched_tickers,
        "mentioned_tickers": [
            {"ticker": m.ticker, "confidence": m.confidence}
            for m in r.mentioned_tickers
        ],
        "reason": r.reason,
    })


class SalienceScorer:
    """Wraps any sync/async LLM call. Caches results in Redis."""

    def __init__(
        self,
        *,
        redis: aioredis.Redis,
        llm_call,  # Callable[[str], str | Awaitable[str]]
        cache_ttl_seconds: int,
    ) -> None:
        self._redis = redis
        self._llm = llm_call
        self._ttl = cache_ttl_seconds

    async def _invoke_llm(self, prompt: str) -> str:
        # Run the sync call off the event-loop thread so blocking LLM/embed I/O
        # (HTTP round-trip, local-model inference, etc.) never stalls the loop.
        # asyncio.to_thread propagates exceptions normally, so the
        # deferred-sentinel path in score() keeps working unchanged.
        #
        # Belt-and-braces await-detection (restored): first check whether the
        # callable itself is a coroutine function (async def / async __call__) and
        # if so await it directly.  For everything else run via to_thread, then
        # check whether the returned value is itself awaitable — a sync function
        # that returns a coroutine object (e.g. a factory wrapping an async inner)
        # must be awaited or the coroutine is never executed and _parse receives a
        # coroutine object instead of a str, silently producing source='deferred'.
        import inspect
        if inspect.iscoroutinefunction(self._llm):
            # Async callable (async def function or object with async __call__):
            # await the coroutine directly on the event loop.
            return await self._llm(prompt)
        # Sync callable: dispatch to a worker thread so the event loop stays live.
        out = await asyncio.to_thread(self._llm, prompt)
        # Belt-and-braces: if the sync callable returned an awaitable (e.g. a
        # coroutine object from an async inner function), await it now so the
        # result is always a plain str before being handed to _parse.
        if hasattr(out, "__await__"):
            out = await out
        return out

    async def score(
        self,
        *,
        env: Envelope,
        watchlist: Sequence[str],
        macro_context: str,
    ) -> SalienceResult:
        key = _cache_key(env)
        cached = await self._redis.get(key)
        if cached:
            result = _parse(cached)
            result.source = "cache"
            return result

        prompt = build_salience_prompt(env=env, watchlist=watchlist,
                                       macro_context=macro_context)
        try:
            raw = await self._invoke_llm(prompt)
            result = _parse(raw)
            result.source = "llm"
        except Exception as e:
            # Don't stall the pipeline — return a deferred sentinel.
            # DO NOT cache the failure: a transient LLM error or out-of-range
            # salience should not poison the cache for 24h.
            # salience=0.0 guarantees a deferred event can never cross the
            # promote threshold (old fallback was 0.1).
            # (Task 15 will add full deferred-queue handling.)
            return SalienceResult(
                salience=0.0, matched_tickers=[], mentioned_tickers=[],
                reason=f"deferred: {type(e).__name__}",
                source="deferred",
            )

        await self._redis.setex(key, self._ttl, _serialize(result))
        return result
