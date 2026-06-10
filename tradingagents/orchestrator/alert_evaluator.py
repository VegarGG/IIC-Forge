"""Structured F4 alert strictness evaluator."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from tradingagents.llm_clients.capabilities import get_capabilities
from tradingagents.llm_clients.postprocess import strip_think_blocks


class AlertEvaluationPayload(BaseModel):
    decision: Literal["pass", "reject"]
    # Bounds enforced via field_validator rather than Field(ge/le) to prevent
    # ``minimum``/``maximum`` from leaking into model_json_schema() output.
    # llama.cpp's GBNF converter chokes on those keys (same pattern as
    # SalienceSchema in tradingagents/sensing/salience.py).
    score: float
    materiality: str
    actionability: str
    ticker_link_evidence: str
    novelty: str
    disqualifiers: List[str] = Field(default_factory=list)
    reasons: List[str] = Field(default_factory=list)

    @field_validator("score")
    @classmethod
    def score_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"score must be in [0.0, 1.0], got {v!r}")
        return v


def alert_evaluation_response_format() -> Dict[str, Any]:
    """Return a ``response_format`` dict for json_schema-mode LLM calls.

    Usage (call-site, e.g. Task 14 harness)::

        fmt = alert_evaluation_response_format()
        response = llm.invoke(prompt, response_format=fmt)

    Binding is capability-gated: ``evaluate_alert_candidate`` calls
    ``get_capabilities(model_id).supports_json_schema`` and attaches this
    format via ``llm.bind(response_format=...)`` only when the resolved model
    supports grammar-constrained decoding.  DeepSeek/MiniMax API models and
    any unrowed model id are left unbound and receive plain free-text prompting.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "AlertEvaluationResult",
            "schema": AlertEvaluationPayload.model_json_schema(),
            "strict": False,
        },
    }


@dataclass(frozen=True)
class AlertEvaluation:
    passed: bool
    score: float
    payload: dict
    disqualifiers: list[str]
    # Telemetry fields (Task 10)
    model_id: Optional[str] = None
    parse_ok: Optional[bool] = None
    latency_ms: Optional[int] = None


def build_alert_evaluation_prompt(*, event_text: str, tickers: list[str]) -> str:
    return (
        "You are the IIC-FORGE alert quality gate. Decide whether this event "
        "is worth sending a light alert to a human investor before any full study. "
        "Reject stale, duplicated, vague, weakly ticker-linked, low-materiality, "
        "or non-actionable events. Pass only when the event has a direct ticker "
        "link and could plausibly change a watchlist thesis or near-term decision.\n\n"
        "Return strict JSON with keys: decision, score, materiality, actionability, "
        "ticker_link_evidence, novelty, disqualifiers, reasons.\n\n"
        f"TICKERS: {', '.join(tickers)}\n\n"
        f"EVENT:\n{event_text[:5000]}"
    )


def _resolve_model_id(llm: Any, model_id: Optional[str]) -> Optional[str]:
    """Resolve model identity: explicit kwarg > llm.model_name > None."""
    if model_id is not None:
        return model_id
    return getattr(llm, "model_name", None)


def evaluate_alert_candidate(
    *,
    llm: Any,
    event_text: str,
    tickers: list[str],
    min_score: float,
    model_id: Optional[str] = None,
) -> AlertEvaluation:
    prompt = build_alert_evaluation_prompt(event_text=event_text, tickers=tickers)
    resolved_model_id = _resolve_model_id(llm, model_id)

    # Capability-gated: bind json_schema response_format only for this call
    # when the resolved model supports grammar-constrained decoding.  The bind
    # creates a new RunnableBinding for THIS invocation only — the caller's
    # original ``llm`` object is never mutated, so a shared llm (e.g. the
    # Secretary's free-text path in the promoter) remains unbound.
    # model_id=None (no capability info) → skip bind, plain invoke.
    # hasattr guard: plain test doubles / fake LLMs that lack .bind() are left
    # unbound, preserving backward compatibility for existing test fixtures.
    call_llm = llm
    if (
        resolved_model_id
        and get_capabilities(resolved_model_id).supports_json_schema
        and hasattr(llm, "bind")
    ):
        call_llm = llm.bind(response_format=alert_evaluation_response_format())

    t0 = time.monotonic()
    latency_ms: Optional[int] = None
    try:
        resp = call_llm.invoke(prompt)
        latency_ms = int((time.monotonic() - t0) * 1000)
        raw = strip_think_blocks(getattr(resp, "content", str(resp)))
        payload = AlertEvaluationPayload.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
        if latency_ms is None:
            latency_ms = int((time.monotonic() - t0) * 1000)
        return AlertEvaluation(
            passed=False,
            score=0.0,
            payload={"decision": "reject", "score": 0.0},
            disqualifiers=["invalid_json"],
            model_id=resolved_model_id,
            parse_ok=False,
            latency_ms=latency_ms,
        )

    passed = (
        payload.decision == "pass"
        and payload.score >= min_score
        and not payload.disqualifiers
    )
    return AlertEvaluation(
        passed=passed,
        score=payload.score,
        payload=payload.model_dump(),
        disqualifiers=list(payload.disqualifiers),
        model_id=resolved_model_id,
        parse_ok=True,
        latency_ms=latency_ms,
    )
