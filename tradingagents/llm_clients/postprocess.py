"""Response-side post-processing helpers for LLM output.

strip_think_blocks
------------------
Belt-and-suspenders guard for GGUF/local templates that ignore
``enable_thinking=False`` and embed ``<think>...</think>`` blocks
directly in ``message.content``.

Placement: this module is provider-adjacent (llm_clients package) and
importable by both parse-path consumers without import cycles:
  - tradingagents/sensing/salience.py
  - tradingagents/orchestrator/alert_evaluator.py
Both already import from llm_clients; llm_clients does NOT import from
sensing or orchestrator, so no cycle is introduced.

Unclosed-block design choice
-----------------------------
Only *closed* ``<think>...</think>`` pairs are stripped.  If a model emits
``<think>`` but never closes before the JSON, the text is left untouched
and ``json.loads`` will subsequently fail → ``parse_ok=False`` / deferred
sentinel.  We prefer a safe, visible failure over silently corrupting an
ambiguously truncated payload.  This is the minimal-surprise option.

Apply on the classification parse paths only (salience._parse and
alert_evaluator.evaluate_alert_candidate) — NOT on synthesis paths,
which never go through a local model.
"""

from __future__ import annotations

import re

# Matches one closed <think>...</think> pair.  DOTALL so newlines inside the
# block are captured.  IGNORECASE so <THINK> / <Think> are handled equally.
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def strip_think_blocks(text: str) -> str:
    """Remove all closed ``<think>...</think>`` blocks from *text*.

    After removal, leading/trailing whitespace is stripped so that the
    caller can hand the result directly to ``json.loads`` without an
    extra ``.strip()`` call.

    Unclosed blocks (no matching ``</think>``) are intentionally left
    intact — see module docstring for the rationale.

    Parameters
    ----------
    text:
        Raw LLM response content, potentially prefixed with think blocks.

    Returns
    -------
    str
        The text with all closed think-block pairs removed and then
        ``.strip()``-ed.
    """
    return _THINK_RE.sub("", text).strip()
