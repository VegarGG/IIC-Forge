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
import inspect
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import redis.asyncio as aioredis
from pydantic import BaseModel, field_validator

from tradingagents.llm_clients.postprocess import strip_think_blocks

import logging

from .envelope import Envelope
from .prompts import build_salience_prompt

log = logging.getLogger(__name__)


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
    latency_ms: Optional[int] = None


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

    Binding is handled by ``maybe_bind_salience_schema`` (below), which is
    called in ``triage._main`` immediately after ``create_role_llm``.  The
    bind is capability-gated: only models with ``supports_json_schema=True``
    in the capability table receive the format; all others are left unbound.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "SalienceResult",
            "schema": SalienceSchema.model_json_schema(),
            "strict": False,
        },
    }


def maybe_bind_salience_schema(llm: Any, model_id: str) -> Any:
    """Return ``llm.bind(response_format=salience_response_format())`` when the
    model supports json_schema; return the original ``llm`` otherwise.

    This is a capability-gated helper: DeepSeek/MiniMax API models have
    ``supports_json_schema=False`` and are left unbound.  Local GGUF models
    (llama.cpp) have ``supports_json_schema=True`` and receive the grammar
    constraint so invalid JSON is structurally impossible (D4 invariant).

    Args:
        llm:      A LangChain-compatible chat model (e.g. ChatOpenAI).
        model_id: The model identifier used to look up capabilities.

    Returns:
        A ``RunnableBinding`` (from ``.bind()``) when json_schema is supported,
        or the original ``llm`` when it is not.
    """
    from tradingagents.llm_clients.capabilities import get_capabilities, is_default_caps
    caps = get_capabilities(model_id) if model_id else None
    if caps is not None and caps.supports_json_schema and hasattr(llm, "bind"):
        return llm.bind(response_format=salience_response_format())
    if model_id and caps is not None and is_default_caps(caps):
        log.warning(
            "json_schema binding skipped for model %s (no capability row) "
            "— local grammar enforcement inactive",
            model_id,
        )
    return llm


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
    # Strip <think>...</think> blocks first (local GGUF belt-and-suspenders),
    # then strip markdown code fences (API responses may wrap JSON).
    # Order matters: think-strip must come before fence-strip in case a model
    # emits <think>…</think>```json\n{…}\n``` — after think-strip only the
    # fence remains for _strip_fences to handle.
    after_think = strip_think_blocks(blob)
    cleaned = _strip_fences(after_think)
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
        # Provider metadata for ledger records (Task 5).  Set by triage._main
        # after building the quick_client; defaults allow unit tests to
        # construct SalienceScorer without wiring a real LLM client.
        self.provider: str = "unknown"
        self.model_id: str = "unknown"
        self.base_url: Optional[str] = None
        self.fallback_mode: Optional[str] = None
        self.fallback_used: bool = False

    async def _invoke_llm(self, prompt: str) -> str:
        # Run the sync call off the event-loop thread so blocking LLM/embed I/O
        # (HTTP round-trip, local-model inference, etc.) never stalls the loop.
        # asyncio.to_thread propagates exceptions normally, so the
        # deferred-sentinel path in score() keeps working unchanged.
        #
        # Belt-and-braces await-detection: first check whether the callable is
        # a plain async def coroutine function via inspect.iscoroutinefunction.
        # NOTE: objects with an async __call__ method return False from
        # iscoroutinefunction — they take the to_thread branch below and are
        # rescued by the __await__ fallback.  Only bare async def functions (or
        # callables whose __call__ passes iscoroutinefunction) reach the direct-
        # await branch.  Either way the result is a plain str before _parse.
        if inspect.iscoroutinefunction(self._llm):
            # Plain async def function: await the coroutine directly on the event loop.
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
            try:
                result = _parse(cached)
                result.source = "cache"
                return result
            except Exception as _cache_err:
                # Legacy blob from a prior branch/version (e.g. out-of-range
                # salience=7.5 cached before the bounds validator was tightened)
                # must not dead-letter the event.  Treat as a cache MISS and
                # fall through to live LLM scoring so the event is processed.
                log.debug(
                    "cache parse failed for key %s (%s: %s) — treating as miss",
                    key, type(_cache_err).__name__, _cache_err,
                )

        prompt = build_salience_prompt(env=env, watchlist=watchlist,
                                       macro_context=macro_context)
        # Failure handling (Task 15 / D5): don't stall the pipeline — return a
        # deferred sentinel.  DO NOT cache the failure: a transient LLM error
        # or out-of-range salience should not poison the cache for 24h.
        # salience=0.0 guarantees a deferred result can never cross the
        # promote threshold (triage additionally persists deferred events with
        # salience=NULL and salience_source='deferred', and skips dedupe
        # recording so a redelivery is re-scored).  The reason string tags the
        # failure class — 'llm_error' (endpoint/transport/SDK failure) vs
        # 'parse_error' (model answered but the JSON was unusable) — so the
        # availability counter log lines can distinguish endpoint health
        # problems from model-quality problems.
        _t0 = time.perf_counter()
        try:
            raw = await self._invoke_llm(prompt)
            _latency_ms: Optional[int] = int((time.perf_counter() - _t0) * 1000)
        except Exception as e:
            _elapsed_ms = int((time.perf_counter() - _t0) * 1000)
            return SalienceResult(
                salience=0.0, matched_tickers=[], mentioned_tickers=[],
                reason=f"deferred: llm_error: {type(e).__name__}",
                source="deferred",
                latency_ms=_elapsed_ms,
            )
        try:
            result = _parse(raw)
            result.source = "llm"
            result.latency_ms = _latency_ms
        except Exception as e:
            return SalienceResult(
                salience=0.0, matched_tickers=[], mentioned_tickers=[],
                reason=f"deferred: parse_error: {type(e).__name__}",
                source="deferred",
                latency_ms=_latency_ms,
            )

        await self._redis.setex(key, self._ttl, _serialize(result))
        return result
