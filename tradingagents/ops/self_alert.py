"""Operator self-alert seam (Task 17) — "the local LLM endpoint is down".

The plan's FORGE_04 "Phase B alerting seam" never landed, so this module IS
the minimal seam: a ``SelfAlerter`` with a pluggable ``transport`` callable.
The daemons construct one via ``build_self_alerter(config)`` and hand its
``endpoint_down_callback`` to their ``AvailabilityCounter`` as the
``on_threshold`` hook; tests inject a recording transport instead.

Design decisions (documented per the Task 17 spec):
  - DEBOUNCE lives in the counter, not here: ``AvailabilityCounter`` invokes
    ``on_threshold`` exactly once per outage (latched when ``consecutive``
    crosses ``alert_threshold``, re-armed by ``record_success``), so this
    module stays a dumb message pipe.  The callback fires OUTSIDE the
    counter's (possibly shared) lock — see AvailabilityCounter.record_failure.
  - THRESHOLD SOURCE: the daemons reuse the role's existing
    ``fallback_threshold`` config key (llm_roles.<role>.fallback_threshold,
    default 3) as the alert threshold — no new config plumbing.  Unlike the
    fallback ENGAGEMENT (which only happens when fallback="api"), the alert
    arms in every fallback mode: a dead endpoint with fallback="none" is
    exactly when the operator must hear about it.
  - TRANSPORT: Telegram via the EXISTING delivery keys — IIC_TELEGRAM_BOT_TOKEN
    (env) + config["telegram_bot"]["enabled"/"allowed_chat_ids"], reusing
    delivery.telegram's bot cache.  When any of those is missing at runtime
    the alerter degrades to log-only (CRITICAL — journald still surfaces it),
    never raises.  Plain text, no parse_mode: failure reasons contain
    characters Markdown would choke on.
  - The alert is best-effort once: a transport failure is logged (the
    CRITICAL log line already carried the message) but NOT retried, and the
    counter's latch stays set until recovery.
  - FUTURE WORK: no periodic re-alert during a persistent outage (e.g. every
    N hours); one alert per outage, re-armed on recovery, is the contract.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)

Transport = Callable[[str], None]


class SelfAlerter:
    """Sends operator self-alerts through a pluggable transport.

    ``notify`` ALWAYS logs the message at CRITICAL first, so the alert is
    loud in the journal even when no transport is configured or the
    transport itself is down.  Transport exceptions are swallowed (logged):
    a broken operator channel must never take the daemon with it.
    """

    def __init__(self, *, transport: Optional[Transport] = None) -> None:
        self._transport = transport

    def notify(self, message: str) -> None:
        log.critical("SELF-ALERT: %s", message)
        if self._transport is None:
            return
        try:
            self._transport(message)
        except Exception:  # noqa: BLE001 — operator channel is best-effort
            log.exception(
                "self-alert transport failed (message already logged above)")

    def endpoint_down_callback(self, counter) -> None:
        """``AvailabilityCounter.on_threshold`` adapter.

        Invoked by the counter OUTSIDE its lock, exactly once per outage.
        The counter fields are read unlocked — under a concurrent burst they
        may have advanced past the crossing values, which is benign (the
        message is a snapshot for a human).
        """
        self.notify(
            f"local LLM endpoint down: counter={counter.name} "
            f"consecutive={counter.consecutive} total={counter.total} "
            f"last_failure_ts={counter.last_failure_ts} "
            f"last_reason={counter.last_reason}"
        )


def telegram_transport(config: Dict[str, Any]) -> Optional[Transport]:
    """Build a Telegram operator transport from EXISTING config/env keys.

    Reuses the same keys as the delivery channel (no new plumbing):
    ``IIC_TELEGRAM_BOT_TOKEN`` env + ``telegram_bot.enabled`` +
    ``telegram_bot.allowed_chat_ids[0]``.  Returns ``None`` (→ log-only
    alerting) when any is missing — logged once at build time so the
    degradation is visible at daemon startup, not at outage time.
    """
    tg = (config or {}).get("telegram_bot", {}) or {}
    token = os.environ.get("IIC_TELEGRAM_BOT_TOKEN", "")
    allowed = tg.get("allowed_chat_ids") or []
    if not (tg.get("enabled", False) and token and allowed):
        log.warning(
            "self-alert telegram transport not configured "
            "(enabled=%s token_set=%s chat_ids=%d) — self-alerts are LOG-ONLY",
            tg.get("enabled", False), bool(token), len(allowed),
        )
        return None
    chat_id = allowed[0]

    def _send(message: str) -> None:
        # Lazy import + module-attribute access so the telegram dependency is
        # only touched on an actual send and tests can patch _get_bot.
        from tradingagents.delivery import telegram as tg_mod
        bot = tg_mod._get_bot(token)
        tg_mod._run_coro(bot.send_message(chat_id=chat_id, text=message))

    return _send


def build_self_alerter(config: Dict[str, Any]) -> SelfAlerter:
    """The daemons' one-liner: Telegram transport if configured, else log-only."""
    return SelfAlerter(transport=telegram_transport(config))
