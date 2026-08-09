"""Availability-counter alerts with a durable production transport.

``SelfAlerter`` remains a small injectable seam so unit tests can provide a
recording transport. ``build_self_alerter`` is the production constructor: it
persists a deduplicated operational alert and queues Telegram/email delivery
through the leased outbox. Direct Telegram sending remains only as a legacy
compatibility helper and is not used by production construction.

The availability counter owns threshold/debounce behavior and calls this seam
outside its lock. Context identifies the role/provider/model/endpoint. Every
notification is logged at CRITICAL before persistence is attempted; a database
failure is therefore loud but never crashes the monitored daemon.
"""

from __future__ import annotations

import asyncio
import hashlib
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

        The headline is provider-aware: "local LLM endpoint down" when the
        context contains ``provider=local``; "LLM endpoint down" otherwise.
        The full context (provider, model, endpoint) is always included.
        """
        # Derive provider from the context string so the headline is truthful.
        import re as _re
        _m = _re.search(r"provider=(\S+)", self._context)
        _provider = _m.group(1).lower() if _m else ""
        if _provider == "local":
            headline = "local LLM endpoint down"
        else:
            headline = "LLM endpoint down"
        self.notify(
            f"{headline}: counter={counter.name} "
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
    """Build a durable self-alert routed through the normal delivery outbox.

    The legacy ``telegram_transport`` remains available as an injectable seam
    for older callers, but production construction never performs a direct
    best-effort network send. A delivery outage therefore leaves queued work.
    """
    stable_context = context.strip()
    digest = hashlib.sha256(stable_context.encode("utf-8")).hexdigest()[:24]

    def _record(message: str) -> None:
        from tradingagents.ops.alerts import record_operational_alert
        from tradingagents.persistence.db import connect

        conn = connect(str(config["iic_db_path"]))
        try:
            record_operational_alert(
                conn,
                config=config,
                dedup_key=f"llm-endpoint:{digest}",
                category="llm_endpoint",
                severity="critical",
                summary=message,
                details={"source": "availability_counter"},
            )
        finally:
            conn.close()

    return SelfAlerter(transport=_record, context=context)
