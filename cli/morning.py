"""`forge morning-digest` and `forge digest tail` sub-commands.

The manual command composes immediately but uses the same durable delivery
outbox as the production scheduler. ``--dry-run`` omits the outbox rows.
"""

from __future__ import annotations

import sys
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Tuple
from zoneinfo import ZoneInfo

import typer

from tradingagents import default_config as _dc
from tradingagents.persistence.db import connect as iic_connect


def _config() -> dict:
    return _dc.DEFAULT_CONFIG


morning_app = typer.Typer(name="morning-digest", help="Morning digest scheduling and tail")
digest_app = typer.Typer(name="digest", help="Digest helpers")


def _build_secretary(config: dict) -> Tuple[Any, sqlite3.Connection]:
    from tradingagents.llm_clients.factory import create_llm_client
    from tradingagents.secretary.service import Secretary

    llm = create_llm_client(
        provider=config["llm_provider"],
        model=config["deep_think_llm"],
        base_url=config.get("backend_url"),
    ).get_llm()
    conn = iic_connect(config["iic_db_path"])
    sec = Secretary(conn=conn, data_dir=config["iic_data_dir"], llm=llm)
    return sec, conn


@morning_app.command("now")
def morning_digest_now(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Compose but skip durable delivery enqueue"
    ),
) -> None:
    config = _config()
    sec, conn = _build_secretary(config)
    zone_name = config["delivery"]["quiet_hours"]["timezone"]
    ts = datetime.now(ZoneInfo(zone_name)).isoformat()
    brief_id = sec.compose_morning_digest(
        watchlist=None, ts=ts, deliver=not dry_run
    )
    typer.echo(f"morning_digest brief composed: {brief_id}")

    if dry_run:
        typer.echo("dry-run: skipping delivery enqueue")
        return
    queued = conn.execute(
        "SELECT COUNT(*) FROM delivery_queue WHERE brief_id = ?", (brief_id,)
    ).fetchone()[0]
    typer.echo(f"queued {queued} durable delivery job(s)")


@digest_app.command("tail")
def digest_tail() -> None:
    """Print the most recent morning_digest brief content to stdout."""
    config = _config()
    conn = iic_connect(config["iic_db_path"])
    row = conn.execute(
        "SELECT content_path FROM briefs WHERE mode = 'morning_digest' "
        "ORDER BY generated_ts DESC LIMIT 1"
    ).fetchone()
    if row is None:
        typer.echo("no morning_digest briefs found", err=True)
        raise typer.Exit(1)
    body_path = Path(config["iic_data_dir"]) / row[0]
    sys.stdout.write(body_path.read_text())
