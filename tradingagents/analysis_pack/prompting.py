from __future__ import annotations

from typing import Any

from tradingagents.security.untrusted import render_untrusted_payload


def render_pack_for_followup(pack: dict[str, Any], *, max_chars: int = 12000) -> str:
    content = pack["content"]
    lines = [
        f"Prior analysis pack for {content.get('ticker', pack.get('ticker'))}",
        f"Trade date: {content.get('trade_date', pack.get('trade_date'))}",
        "",
        "Event context:",
        content.get("event_context", ""),
        "",
        "Market snapshot:",
        content.get("market_snapshot", ""),
        "",
        "Key reports:",
    ]
    for key, body in content.get("reports", {}).items():
        lines += [f"## {key}", str(body)]
    lines += ["", "Prior final decisions:"]
    for item in content.get("final_trade_decisions", []):
        lines += [
            f"## {item.get('persona_id', 'analysis')} {item.get('decision', '')}",
            item.get("body", ""),
        ]
    return render_untrusted_payload(
        "prior_analysis_pack",
        "\n".join(lines),
        max_chars=max_chars,
    )
