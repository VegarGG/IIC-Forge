"""Tests for strip_think_blocks — the belt-and-suspenders response-side stripper.

Placement rationale
-------------------
``strip_think_blocks`` lives in ``tradingagents/llm_clients/postprocess.py``.
It is provider-adjacent and importable by both consumers
(``sensing/salience.py`` and ``orchestrator/alert_evaluator.py``) without
creating an import cycle: sensing/orchestrator already import from llm_clients;
llm_clients does NOT import from sensing or orchestrator.

Unclosed-block design choice
-----------------------------
Only *closed* ``<think>...</think>`` pairs are stripped.  If the model emits
``<think>...`` and never closes before the JSON, the resulting text will still
contain ``<think>`` and will fail ``json.loads`` → ``parse_ok=False`` /
``source="deferred"``.  That is the safe-failure mode: we prefer a silent
deferral over silently corrupting the payload.  This choice is documented in
the helper's docstring and asserted in ``test_unclosed_block_left_intact``.
"""

from __future__ import annotations

import json
import pytest

from tradingagents.llm_clients.postprocess import strip_think_blocks


# ---------------------------------------------------------------------------
# Unit tests for the helper
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_single_closed_block_stripped():
    raw = '<think>reasoning…</think>{"salience": 0.9}'
    result = strip_think_blocks(raw)
    assert "<think>" not in result.lower()
    data = json.loads(result)
    assert data["salience"] == pytest.approx(0.9)


@pytest.mark.unit
def test_text_without_think_blocks_unchanged():
    raw = '{"salience": 0.5}'
    assert strip_think_blocks(raw) == raw


@pytest.mark.unit
def test_multiple_closed_blocks_all_stripped():
    raw = '<think>first</think><think>second</think>{"ok": true}'
    result = strip_think_blocks(raw)
    assert "<think>" not in result.lower()
    assert json.loads(result) == {"ok": True}


@pytest.mark.unit
def test_dotall_newlines_inside_block():
    raw = '<think>\nreasoning\nacross\nlines\n</think>{"salience": 0.7}'
    result = strip_think_blocks(raw)
    assert "<think>" not in result.lower()
    assert json.loads(result)["salience"] == pytest.approx(0.7)


@pytest.mark.unit
def test_ignorecase_uppercase_think():
    raw = '<THINK>uppercase reasoning</THINK>{"salience": 0.3}'
    result = strip_think_blocks(raw)
    assert "<think>" not in result.lower()
    assert json.loads(result)["salience"] == pytest.approx(0.3)


@pytest.mark.unit
def test_unclosed_block_left_intact():
    """Unclosed <think> is intentionally NOT stripped — json.loads will fail,
    which propagates to the safe deferred/parse_ok=False path.  See module
    docstring for the design rationale."""
    raw = '<think>model forgot to close this{"salience": 0.5}'
    result = strip_think_blocks(raw)
    # The unclosed block remains; json.loads should fail.
    assert "<think>" in result.lower()
    with pytest.raises(json.JSONDecodeError):
        json.loads(result)


@pytest.mark.unit
def test_leading_whitespace_stripped_after_removal():
    raw = '<think>x</think>   \n   {"salience": 0.2}'
    result = strip_think_blocks(raw)
    assert result.startswith("{"), f"Leading whitespace not stripped: {result!r}"


@pytest.mark.unit
def test_combined_fence_and_think_blocks():
    """Think strip then fence strip (salience._parse order).

    Input: <think>…</think>```json\n{…}\n```
    After think-strip:       ```json\n{…}\n```
    After fence-strip:       {…}
    """
    raw = '<think>reasoning</think>```json\n{"salience": 0.6}\n```'
    # Simulate the salience._parse pipeline: think-strip first, then fence-strip.
    from tradingagents.sensing.salience import _strip_fences
    after_think = strip_think_blocks(raw)
    assert "```" in after_think   # fences still present
    after_fence = _strip_fences(after_think)
    data = json.loads(after_fence)
    assert data["salience"] == pytest.approx(0.6)


# ---------------------------------------------------------------------------
# Integration: salience._parse path
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_salience_parse_strips_think_before_json():
    """The internal _parse function handles think-prefixed JSON directly."""
    from tradingagents.sensing.salience import _parse
    raw = '<think>internal reasoning</think>{"salience": 0.9, "matched_tickers": [], "mentioned_tickers": [], "reason": ""}'
    result = _parse(raw)
    assert result.salience == pytest.approx(0.9)


@pytest.mark.unit
async def test_salience_scorer_source_llm_with_think_prefix():
    """SalienceScorer.score returns source=='llm' when LLM emits think-prefixed JSON."""
    import fakeredis.aioredis
    from datetime import datetime, timezone
    from tradingagents.sensing.envelope import Envelope
    from tradingagents.sensing.salience import SalienceScorer

    env = Envelope(
        source="polygon_news",
        ingested_ts=datetime.now(timezone.utc).isoformat(),
        external_id="x:think1",
        text="AAPL beats expectations",
        source_tags={},
        raw_path="p",
    )

    def fake_llm(_prompt: str) -> str:
        return (
            '<think>Let me reason about salience...</think>'
            '{"salience": 0.9, "matched_tickers": ["AAPL"], '
            '"mentioned_tickers": [{"ticker": "AAPL", "confidence": 0.95}], '
            '"reason": "direct beat"}'
        )

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    scorer = SalienceScorer(redis=r, llm_call=fake_llm, cache_ttl_seconds=86400)
    result = await scorer.score(env=env, watchlist=["AAPL"], macro_context="")
    assert result.source == "llm"
    assert result.salience == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Integration: alert_evaluator parse path
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_alert_evaluator_parse_ok_with_think_prefix():
    """evaluate_alert_candidate returns parse_ok=True when LLM emits think-prefixed JSON."""
    from types import SimpleNamespace
    from tradingagents.orchestrator.alert_evaluator import evaluate_alert_candidate

    valid_payload = (
        '{"decision":"pass","score":0.88,"materiality":"earnings surprise",'
        '"actionability":"watchlist thesis may change",'
        '"ticker_link_evidence":"NVDA named directly","novelty":"new filing",'
        '"disqualifiers":[],"reasons":["direct and material"]}'
    )

    class ThinkFakeLLM:
        def invoke(self, _prompt):
            return SimpleNamespace(
                content=f"<think>my deep reasoning</think>{valid_payload}"
            )

    result = evaluate_alert_candidate(
        llm=ThinkFakeLLM(),
        event_text="NVDA raises guidance after earnings.",
        tickers=["NVDA"],
        min_score=0.80,
    )
    assert result.parse_ok is True
    assert result.passed is True
    assert result.score == pytest.approx(0.88)


@pytest.mark.unit
def test_no_think_blocks_in_response_l2_assertion():
    """L2 harness assertion: responses from classification paths must not contain
    <think> blocks after processing.  Verifies the stripper removes ALL occurrences."""
    multi_block = (
        "<think>first block</think>"
        "<think>second block</think>"
        '{"salience": 0.5, "matched_tickers": [], "mentioned_tickers": [], "reason": ""}'
    )
    from tradingagents.sensing.salience import _parse
    result = _parse(multi_block)
    # The output went through strip → there should be no parse error
    assert result.salience == pytest.approx(0.5)
    # Confirm the helper itself guarantees no think blocks remain (closed ones)
    stripped = strip_think_blocks(multi_block)
    assert "<think>" not in stripped.lower()
