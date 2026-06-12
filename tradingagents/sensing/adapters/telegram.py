"""Telegram sensing adapter — Telethon NewMessage streaming.

Uses a SEPARATE session from the F0 OSINT pull path. Two session files
exist because Telethon kicks a second concurrent connection on the same
session.

Cursor: JSON dict mapping channel username → max message_id seen.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import redis.asyncio as aioredis

from tradingagents.sensing.adapters.base import EnvelopeWriter
from tradingagents.sensing.cursor import CursorStore
from tradingagents.sensing.envelope import Envelope
from tradingagents.sensing.source_health import record_poll_success


log = logging.getLogger(__name__)
NAME = "telegram"
HEARTBEAT_INTERVAL_SECONDS = 300


def _ensure_session_dir(session_path: str) -> None:
    """Create the parent directory for the Telethon session file if missing.

    A missing /data/telegram directory must not crash-loop the adapter.
    """
    Path(session_path).parent.mkdir(parents=True, exist_ok=True)


async def _on_message(event, *, redis, conn, stream: str, staging_root: str) -> None:
    msg = event.message
    text = (msg.message or "").strip()
    if not text:
        return
    channel = getattr(event.chat, "username", None) or "unknown"
    cs = CursorStore(conn)
    cursors = json.loads(cs.get(NAME) or "{}")
    cursors[channel] = max(int(cursors.get(channel, 0)), int(msg.id))
    env = Envelope(
        source=NAME,
        ingested_ts=datetime.now(timezone.utc).isoformat(),
        external_id=f"tg:{channel}:{msg.id}",
        text=text,
        source_tags={"channel": channel,
                     "msg_date": msg.date.isoformat()},
        raw_path="",
    )
    writer = EnvelopeWriter(source=NAME, redis=redis, conn=conn,
                             stream=stream, staging_root=staging_root)
    await writer.write(env, raw_payload={"channel": channel,
                                          "message_id": msg.id,
                                          "text": text},
                       cursor=json.dumps(cursors))
    try:
        record_poll_success(
            conn,
            source=NAME,
            service_name="adapter-telegram",
            emitted=1,
            cursor=json.dumps(cursors),
            last_event_ts=datetime.now(timezone.utc).isoformat(),
            diagnostics={"resolved_channels": sorted(cursors.keys())},
        )
    except Exception:
        log.exception("telegram: health write failed (non-fatal)")


def _write_heartbeat(conn: sqlite3.Connection, cursors: dict) -> None:
    """Write a heartbeat health row so the gate's sources_fresh check passes
    on healthy-but-quiet channels that have not emitted a message recently.

    Uses ``cursor=None`` and ``last_event_ts=None`` so the store's COALESCE
    preserves any real cursor value and last_event_ts already in the row.
    """
    record_poll_success(
        conn,
        source=NAME,
        service_name="adapter-telegram",
        emitted=0,
        cursor=None,
        last_event_ts=None,
        diagnostics={
            "resolved_channels": sorted(cursors.keys()),
            "heartbeat": True,
        },
    )


async def _heartbeat_loop(
    conn: sqlite3.Connection,
    get_cursors,
    interval: int = HEARTBEAT_INTERVAL_SECONDS,
) -> None:
    """Asyncio task: write a heartbeat row every ``interval`` seconds.

    ``get_cursors`` is a zero-argument callable that returns the current
    cursors dict (captured by the outer scope so it reflects live state).
    Non-fatal: logs exceptions and continues.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            _write_heartbeat(conn, get_cursors())
            log.debug("telegram heartbeat written (channels=%s)", sorted(get_cursors().keys()))
        except Exception:
            log.exception("telegram: heartbeat write failed (non-fatal)")


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    from tradingagents.default_config import DEFAULT_CONFIG as C
    from tradingagents.persistence.db import connect
    from tradingagents.sensing.redis_client import make_redis

    if not C["sensing_adapters_enabled"].get(NAME, True):
        log.info("%s disabled; exiting 0", NAME); return

    api_id = os.environ.get("TELEGRAM_API_ID")
    api_hash = os.environ.get("TELEGRAM_API_HASH")
    if not (api_id and api_hash):
        # Exit CLEANLY (0), not SystemExit(1): a non-zero exit trips
        # systemd Restart=on-failure → NRestarts>0 false-FAIL gate. Missing
        # creds is a config gap, not a crash — log loudly and return.
        log.warning("TELEGRAM_API_ID/HASH not set; %s adapter disabled, exiting 0", NAME)
        return
    session = os.environ.get("TELEGRAM_SENSING_SESSION", "iic_sensing.session")

    from telethon import TelegramClient, events  # lazy import

    channels: List[str] = list(C.get("telegram_channels") or [])
    if not channels:
        log.warning("telegram_channels config empty; nothing to listen to")

    redis = make_redis(C["sensing_redis_url"])
    conn = connect(C["iic_db_path"])
    staging = os.path.join(C["iic_data_dir"], "events", "staging")

    _ensure_session_dir(session)
    client = TelegramClient(session, int(api_id), api_hash)

    # Live cursors dict shared between the message handler and heartbeat task.
    # Loaded once at startup from the cursor store; updated on each message.
    cs = CursorStore(conn)
    _cursors: dict = json.loads(cs.get(NAME) or "{}")

    @client.on(events.NewMessage(chats=channels))
    async def handler(event):
        nonlocal _cursors
        try:
            await _on_message(event, redis=redis, conn=conn,
                              stream=C["sensing_ingest_stream"],
                              staging_root=staging)
            # Keep the shared cursors dict up-to-date for the heartbeat.
            _cursors = json.loads(CursorStore(conn).get(NAME) or "{}")
        except Exception:
            log.exception("telegram handler crashed (event dropped, will continue)")

    log.info("telegram sensing adapter started; channels=%s", channels)

    # Reconnect loop with bounded exponential backoff. Telethon's
    # run_until_disconnected() returns on ANY disconnect; without this loop
    # the process would exit 0 and systemd (Restart=on-failure) would NOT
    # restart it, silently going dark for the rest of the soak. Mirrors the
    # resilience pattern in rss.py / polygon_news.py stream() loops.
    import time as _time

    backoff = 1
    _heartbeat_task_handle = None
    while True:
        try:
            client.start()  # interactive prompt only if session is brand-new
            backoff = 1      # connected OK; reset backoff

            # Schedule the heartbeat task inside Telethon's event loop.
            # It is cancelled before each reconnect to avoid duplicate tasks.
            loop = client.loop
            _heartbeat_task_handle = loop.create_task(
                _heartbeat_loop(conn, lambda: _cursors)
            )
            try:
                client.run_until_disconnected()
            finally:
                # Cancel heartbeat on disconnect/reconnect.
                if _heartbeat_task_handle is not None:
                    _heartbeat_task_handle.cancel()
                    _heartbeat_task_handle = None

            # Returned cleanly == disconnected. Loop to reconnect.
            log.warning("telegram disconnected; reconnecting in %ds", backoff)
        except KeyboardInterrupt:
            log.info("telegram adapter interrupted; shutting down")
            return
        except Exception:
            log.exception("telegram connection error; reconnecting in %ds", backoff)
        _time.sleep(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    _main()
