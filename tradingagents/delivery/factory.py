"""Delivery-channel construction without Secretary coupling."""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from tradingagents.delivery.base import DeliveryChannel


def build_channel(
    name: str,
    conn: sqlite3.Connection,
    config: dict[str, Any],
) -> Optional[DeliveryChannel]:
    if name == "cli":
        from tradingagents.delivery.cli import CLIOutbound

        return CLIOutbound(conn=conn, config=config)
    if name == "email":
        from tradingagents.delivery.email import EmailOutbound

        return EmailOutbound(conn=conn, config=config)
    if name == "telegram":
        from tradingagents.delivery.telegram import TelegramOutbound

        return TelegramOutbound(conn=conn, config=config)
    return None
