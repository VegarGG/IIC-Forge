"""Telegram outbound channel — send_message via the Bot API.

Reads bot token from env IIC_TELEGRAM_BOT_TOKEN. allowed_chat_ids[0] is the
destination. Inline keyboards: [Run Backtest]/[Dismiss] for mode='event_alert';
per-ticker [Study <T>] + [Study all]/[Dismiss all] for mode='event_alert_light'.

The polling loop for incoming updates is a separate process (Task 13).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Dict

from tradingagents.delivery.base import DeliveryChannel, DeliveryError
from tradingagents.persistence import store


_BOT_CACHE: dict = {}
_TELEGRAM_TEXT_LIMIT = 4096


def _get_bot(token: str):
    """Lazy import + cache. Returns a python-telegram-bot Bot instance."""
    if token in _BOT_CACHE:
        return _BOT_CACHE[token]
    from telegram import Bot

    bot = Bot(token=token)
    _BOT_CACHE[token] = bot
    return bot


def _safe_markdown_text(body: str) -> str:
    """Escape arbitrary text for MarkdownV2 and keep it within Bot API limits."""
    from telegram.helpers import escape_markdown

    escaped = escape_markdown(body, version=2)
    if len(escaped) <= _TELEGRAM_TEXT_LIMIT:
        return escaped
    shortened = escaped[: _TELEGRAM_TEXT_LIMIT - 1]
    # Cutting an escape sequence after its backslash would make the entire
    # message invalid. Removing trailing backslashes is safe for plain text.
    shortened = shortened.rstrip("\\")
    return f"{shortened}…"


def _make_event_alert_keyboard(brief_id: str):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Run Backtest",
                    callback_data=f"act:{brief_id}:run_backtest:yes",
                ),
                InlineKeyboardButton(
                    "Dismiss",
                    callback_data=f"act:{brief_id}:run_backtest:no",
                ),
            ]
        ]
    )


def _make_light_alert_keyboard(brief_id: str, tickers: list[str]):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    # One [Study <T>] button per affected ticker, two per row.
    cur: list = []
    for t in tickers:
        cur.append(
            InlineKeyboardButton(
                f"Study {t}",
                callback_data=f"act:{brief_id}:run_full_study:{t}",
            )
        )
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append(
        [
            InlineKeyboardButton(
                "✅ Study all", callback_data=f"act:{brief_id}:run_full_study:__all__"
            ),
            InlineKeyboardButton(
                "✖ Dismiss all",
                callback_data=f"act:{brief_id}:run_full_study:__dismiss__",
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


class TelegramOutbound(DeliveryChannel):
    channel_name = "telegram"

    def send(self, *, brief: Dict[str, Any], mode: str, body: str) -> int:
        if mode in {"event_alert", "event_alert_light"}:
            return super().send(brief=brief, mode=mode, body=body)
        cfg = self._config["telegram_bot"]
        if not cfg.get("enabled", False) or not cfg.get("allowed_chat_ids"):
            return store.insert_delivery(
                self._conn,
                brief_id=brief["brief_id"],
                channel=self.channel_name,
                status="skipped",
                sent_ts=None,
                channel_ref=None,
                skip_reason="telegram_disabled",
            )
        return super().send(brief=brief, mode=mode, body=body)

    def send_attempt(self, *, brief: Dict[str, Any], mode: str, body: str) -> int:
        cfg = self._config["telegram_bot"]
        if mode not in {"event_alert", "event_alert_light"} and (
            not cfg.get("enabled", False) or not cfg.get("allowed_chat_ids")
        ):
            return store.insert_delivery(
                self._conn,
                brief_id=brief["brief_id"],
                channel=self.channel_name,
                status="skipped",
                sent_ts=None,
                channel_ref=None,
                skip_reason="telegram_disabled",
            )
        return super().send_attempt(brief=brief, mode=mode, body=body)

    def _send_impl(self, brief: Dict[str, Any], mode: str, body: str) -> tuple:
        cfg = self._config["telegram_bot"]
        if not cfg.get("enabled", False):
            raise DeliveryError(
                "Telegram delivery is disabled",
                category="channel_disabled",
            )
        token = os.environ.get("IIC_TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise DeliveryError(
                "IIC_TELEGRAM_BOT_TOKEN not set",
                category="credential_missing",
            )

        allowed = cfg.get("allowed_chat_ids") or []
        if not allowed:
            raise DeliveryError(
                "no Telegram target chat configured; "
                "set TELEGRAM_BOT_ALLOWED_CHAT_IDS",
                category="recipient_missing",
            )
        chat_id = allowed[0]
        bot = _get_bot(token)

        if mode == "event_alert":
            keyboard = _make_event_alert_keyboard(brief["brief_id"])
        elif mode == "event_alert_light":
            keyboard = _make_light_alert_keyboard(
                brief["brief_id"], brief.get("tickers", [])
            )
        else:
            keyboard = None

        from telegram.error import BadRequest, Forbidden, InvalidToken

        try:
            coro = bot.send_message(
                chat_id=chat_id,
                text=_safe_markdown_text(body),
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
            sent = _run_coro(coro)
        except (BadRequest, Forbidden, InvalidToken) as exc:
            raise DeliveryError(
                f"Telegram rejected the delivery: {exc}",
                category="telegram_rejected",
            ) from exc
        return (f"{chat_id}:{sent.message_id}", None)


def _run_coro(coro):
    """Run an async coroutine to completion, regardless of current loop state."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=30)
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)
