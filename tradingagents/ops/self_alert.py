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
  - NON-BLOCKING SEND: when ``_send`` is called from a running event-loop
    thread (triage's process_one runs there), we must NOT block the loop.
    ``_run_coro`` would do ``run_coroutine_threadsafe(...).result(timeout=30)``
    — blocking the only thread that can drive the loop, guaranteeing a 30 s
    freeze per outage.  Instead, ``_send`` detects a running loop and spawns a
    short-lived daemon thread that calls ``asyncio.run(coro)`` with a freshly
    constructed Bot (so it carries its own httpx client and loop, never
    sharing the cached bot's resources across threads).  Fire-and-forget is
    acceptable: the CRITICAL log already carries the message; transport
    delivery is best-effort.
  - The alert is best-effort once: a transport failure is logged (the
    CRITICAL log line already carried the message) but NOT retried, and the
    counter's latch stays set until recovery.
  - CONTEXT FIELD: ``build_self_alerter`` / ``SelfAlerter`` / the callback
    accept an optional ``context`` string (e.g. "role=alert_gate
    provider=local model=qwen3:6b endpoint=http://192.168.1.50:8080/v1")
    prepended to every notification so the operator knows which daemon and
    endpoint died.
  - FUTURE WORK: no periodic re-alert during a persistent outage (e.g. every
    N hours); one alert per outage, re-armed on recovery, is the contract.
    A recovery notice ("endpoint recovered") is also not sent; the operator
    must observe log lines or the counter resetting to zero.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)

Transport = Callable[[str], None]


class SelfAlerter:
    """Sends operator self-alerts through a pluggable transport.

    ``notify`` ALWAYS logs the message at CRITICAL first, so the alert is
    loud in the journal even when no transport is configured or the
    transport itself is down.  Transport exceptions are swallowed (logged):
    a broken operator channel must never take the daemon with it.

    ``context`` (optional) is prepended to every notification so the
    operator knows which endpoint/model triggered the alert.
    """

    def __init__(self, *, transport: Optional[Transport] = None,
                 context: str = "") -> None:
        self._transport = transport
        self._context = context

    def notify(self, message: str) -> None:
        full = f"{self._context} | {message}" if self._context else message
        log.critical("SELF-ALERT: %s", full)
        if self._transport is None:
            return
        try:
            self._transport(full)
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

    Non-blocking design: when called from a running event-loop thread (triage
    runs process_one on the loop thread), a daemon thread is spawned that
    builds a fresh Bot instance and calls ``asyncio.run(coro)`` — never
    blocking the loop.  PTB's Bot.__init__ does not bind to any event loop,
    and HTTPXRequest has no loop affinity either, so a per-call Bot is safe.
    When called from a plain sync context (promoter's main thread has no
    running loop), the coroutine is run inline via ``asyncio.run``.
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

    async def _send_coro(message: str, _token: str, _chat_id) -> None:
        """Send via a freshly constructed Bot — no shared loop state."""
        from telegram import Bot
        bot = Bot(token=_token)
        async with bot:
            await bot.send_message(chat_id=_chat_id, text=message)

    def _send(message: str) -> None:
        try:
            asyncio.get_running_loop()
            # Called from a running event-loop thread (e.g. triage's
            # process_one).  Spawn a daemon thread so the loop is never
            # blocked.  A fresh Bot is built inside the thread so that its
            # httpx client and coroutines run in the thread's own asyncio.run
            # loop — no cross-loop resource sharing.
            threading.Thread(
                target=lambda: asyncio.run(
                    _send_coro(message, token, chat_id)
                ),
                daemon=True,
                name="self-alert-send",
            ).start()
        except RuntimeError:
            # No running loop (promoter's main thread): safe to run inline.
            asyncio.run(_send_coro(message, token, chat_id))

    return _send


def build_self_alerter(config: Dict[str, Any], *,
                       context: str = "") -> SelfAlerter:
    """The daemons' one-liner: Telegram transport if configured, else log-only.

    ``context`` is a free-form string that identifies the daemon/role/endpoint
    for the operator (e.g. "role=alert_gate provider=local
    model=qwen3:6b endpoint=http://192.168.1.50:8080/v1").
    """
    return SelfAlerter(transport=telegram_transport(config), context=context)
