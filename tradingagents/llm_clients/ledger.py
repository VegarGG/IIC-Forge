"""Unified LLM call ledger helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from tradingagents.persistence import store


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(token_usage: Optional[dict[str, Any]]) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    # Accepts either langchain `llm_output["token_usage"]` (prompt_tokens/
    # completion_tokens, DeepSeek prompt_cache_{hit,miss}_tokens) or AIMessage
    # `usage_metadata` (input_tokens/output_tokens). Pass one dict whole;
    # Anthropic nested cache fields must be pre-normalized by the caller.
    if not token_usage:
        return None, None, None, None
    in_tokens = token_usage.get("prompt_tokens") or token_usage.get("input_tokens")
    out_tokens = token_usage.get("completion_tokens") or token_usage.get("output_tokens")
    cache_hit = token_usage.get("prompt_cache_hit_tokens") or token_usage.get("cache_hit_tokens")
    cache_miss = token_usage.get("prompt_cache_miss_tokens") or token_usage.get("cache_miss_tokens")
    return (
        int(in_tokens) if in_tokens is not None else None,
        int(out_tokens) if out_tokens is not None else None,
        int(cache_hit) if cache_hit is not None else None,
        int(cache_miss) if cache_miss is not None else None,
    )


def _usd(provider: str, explicit: Optional[float]) -> Optional[float]:
    if explicit is not None:
        return float(explicit)
    return 0.0 if provider == "local" else None


def record_llm_success(
    conn: sqlite3.Connection,
    *,
    role: str,
    service_name: str,
    provider: str,
    model_id: str,
    base_url: Optional[str],
    request_kind: str,
    linked_type: str,
    linked_id: Optional[str],
    latency_ms: Optional[int],
    parse_ok: Optional[bool],
    fallback_mode: Optional[str],
    fallback_used: bool,
    token_usage: Optional[dict[str, Any]] = None,
    usd_estimate: Optional[float] = None,
) -> int:
    in_tokens, out_tokens, cache_hit, cache_miss = _tokens(token_usage)
    return store.insert_llm_call(
        conn,
        created_ts=_now_iso(),
        role=role,
        service_name=service_name,
        provider=provider,
        model_id=model_id,
        base_url=base_url,
        request_kind=request_kind,
        linked_type=linked_type,
        linked_id=linked_id,
        status="success",
        latency_ms=latency_ms,
        parse_ok=parse_ok,
        fallback_mode=fallback_mode,
        fallback_used=fallback_used,
        in_tokens=in_tokens,
        out_tokens=out_tokens,
        cache_hit_tokens=cache_hit,
        cache_miss_tokens=cache_miss,
        usd_estimate=_usd(provider, usd_estimate),
        error_class=None,
        error_message=None,
    )


def record_llm_error(
    conn: sqlite3.Connection,
    *,
    role: str,
    service_name: str,
    provider: str,
    model_id: str,
    base_url: Optional[str],
    request_kind: str,
    linked_type: str,
    linked_id: Optional[str],
    status: str,
    latency_ms: Optional[int],
    parse_ok: Optional[bool],
    fallback_mode: Optional[str],
    fallback_used: bool,
    exc: BaseException,
    usd_estimate: Optional[float] = None,
) -> int:
    return store.insert_llm_call(
        conn,
        created_ts=_now_iso(),
        role=role,
        service_name=service_name,
        provider=provider,
        model_id=model_id,
        base_url=base_url,
        request_kind=request_kind,
        linked_type=linked_type,
        linked_id=linked_id,
        status=status,
        latency_ms=latency_ms,
        parse_ok=parse_ok,
        fallback_mode=fallback_mode,
        fallback_used=fallback_used,
        in_tokens=None,
        out_tokens=None,
        cache_hit_tokens=None,
        cache_miss_tokens=None,
        usd_estimate=_usd(provider, usd_estimate),
        error_class=type(exc).__name__,
        error_message=str(exc)[:1000],
    )
