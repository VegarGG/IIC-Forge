"""IIC-FORGE operational CLI.

Sub-apps:
  - watchlist : manage the curated watchlist (add / list / remove)
  - sense     : sensing-related ops (seed tickers, status, force sweep)

Wired into the main `tradingagents` CLI by ``cli/main.py``.
"""

from __future__ import annotations

import json
import typer
from rich.console import Console
from rich.table import Table

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.persistence.db import connect


app = typer.Typer(name="forge", help="IIC-FORGE operational commands")
console = Console()


# ---------------------------------------------------------------------
# watchlist sub-app
# ---------------------------------------------------------------------

watchlist_app = typer.Typer(name="watchlist", help="Manage the curated watchlist")
app.add_typer(watchlist_app, name="watchlist")


def _conn():
    # Re-read the env var rather than relying solely on DEFAULT_CONFIG —
    # DEFAULT_CONFIG fixes its values at import time, so tests that set
    # TRADINGAGENTS_IIC_DB_PATH after the first import need a live lookup.
    import os
    db_path = os.environ.get("TRADINGAGENTS_IIC_DB_PATH") or DEFAULT_CONFIG["iic_db_path"]
    return connect(db_path)


@watchlist_app.command("add")
def watchlist_add(ticker: str) -> None:
    """Add a ticker to the user-curated watchlist (never expires)."""
    from tradingagents.sensing.watchlist import add_user
    add_user(_conn(), ticker=ticker.upper())
    console.print(f"[green]added[/green] {ticker.upper()} (user-curated, no TTL)")


@watchlist_app.command("list")
def watchlist_list() -> None:
    """Print the current watchlist."""
    conn = _conn()
    rows = list(conn.execute(
        "SELECT ticker, added_ts, last_briefed, ttl_until, tags "
        "FROM watchlist ORDER BY ticker"
    ))
    if not rows:
        console.print("(watchlist is empty)")
        return
    t = Table("ticker", "added", "last_briefed", "ttl_until", "tags")
    for r in rows:
        tags = ", ".join(json.loads(r["tags"]) if r["tags"] else [])
        t.add_row(r["ticker"], r["added_ts"] or "",
                  r["last_briefed"] or "", r["ttl_until"] or "", tags)
    console.print(t)


@watchlist_app.command("remove")
def watchlist_remove(ticker: str) -> None:
    """Remove a ticker from the watchlist (works for user or auto rows)."""
    conn = _conn()
    n = conn.execute("DELETE FROM watchlist WHERE ticker = ?",
                      (ticker.upper(),)).rowcount
    conn.commit()
    if n:
        console.print(f"[yellow]removed[/yellow] {ticker.upper()}")
    else:
        console.print(f"[dim]{ticker.upper()} not on watchlist[/dim]")


# ---------------------------------------------------------------------
# sense sub-app
# ---------------------------------------------------------------------

from tradingagents.sensing.seed_tickers import seed_all, seed_crypto  # noqa: E402
from tradingagents.sensing.watchlist import sweep_expired  # noqa: E402


sense_app = typer.Typer(name="sense", help="Sensing operational commands")
app.add_typer(sense_app, name="sense")


@sense_app.command("reseed-tickers")
def sense_reseed_tickers(
    no_polygon: bool = typer.Option(False, "--no-polygon",
                                     help="Skip Polygon equity seed (crypto only)"),
) -> None:
    """Repopulate the `tickers` reference table.

    Without `--no-polygon`, calls Polygon `/v3/reference/tickers` (requires
    POLYGON_API_KEY). With `--no-polygon`, only seeds the crypto static list.
    """
    conn = _conn()
    if no_polygon:
        n = seed_crypto(conn)
        console.print(f"crypto: {n} rows")
    else:
        result = seed_all(conn)
        console.print(f"crypto: {result['crypto']} rows; polygon: {result['polygon']} rows")


@sense_app.command("sweep-watchlist")
def sense_sweep_watchlist() -> None:
    """One-shot prune of expired auto-watchlist entries."""
    conn = _conn()
    n = sweep_expired(conn)
    console.print(f"pruned {n} expired watchlist row(s)")


# ---------------------------------------------------------------------
# orchestrator sub-app (F4)
# ---------------------------------------------------------------------

orch_app = typer.Typer(name="orchestrator", help="F4 promoter + worker controls")
app.add_typer(orch_app, name="orchestrator")


@orch_app.command("promoter")
def orchestrator_promoter() -> None:
    """Run the promoter loop in the foreground (systemd wraps this)."""
    import logging
    from tradingagents.orchestrator.promoter import main
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    main()


@orch_app.command("worker")
def orchestrator_worker() -> None:
    """Run the worker loop in the foreground (systemd wraps this)."""
    import logging
    from tradingagents.orchestrator.worker import main
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    main()


@orch_app.command("status")
def orchestrator_status() -> None:
    """Quick view of queue depth + recent jobs + today's spend."""
    from tradingagents.orchestrator import queue_store
    conn = _conn()
    pending = queue_store.pending_count(conn)
    today_enqueued = queue_store.daily_enqueue_count(conn)
    today_cost = queue_store.daily_cost_total(conn)

    console.print(f"pending (queued+running): [bold]{pending}[/bold]")
    console.print(f"enqueued today          : {today_enqueued}")
    console.print(f"spend today (USD)       : ${today_cost:.4f}")

    rows = list(conn.execute(
        "SELECT job_id, job_type, state, enqueued_ts, finished_ts, "
        "brief_id, cost_usd, error, error_category, worker_pid, operator_note "
        "FROM queue_jobs ORDER BY job_id DESC LIMIT 10"
    ))
    if not rows:
        console.print("(no jobs)")
        return
    t = Table(
        "id", "type", "state", "pid", "category", "enqueued", "finished",
        "brief", "$", "err/note",
    )
    for r in rows:
        t.add_row(
            str(r["job_id"]), r["job_type"], r["state"],
            str(r["worker_pid"] or ""),
            (r["error_category"] or "")[:20],
            (r["enqueued_ts"] or "")[:19],
            (r["finished_ts"] or "")[:19],
            (r["brief_id"] or "")[:8],
            f"{(r['cost_usd'] or 0.0):.4f}",
            (r["operator_note"] or r["error"] or "")[:40],
        )
    console.print(t)


@orch_app.command("retry")
def orchestrator_retry(
    job_id: int,
    note: str = typer.Option(
        "manual operator retry",
        "--note",
        help="Audit note explaining why this job is being requeued.",
    ),
) -> None:
    """Give one exhausted or blocked analysis job one additional attempt."""
    from tradingagents.orchestrator import queue_store

    if not note.strip():
        raise typer.BadParameter("--note must not be empty")
    if not queue_store.retry_error_job(
        _conn(), job_id=job_id, operator_note=note
    ):
        raise typer.BadParameter(
            f"job {job_id} is not in error or blocked state"
        )
    console.print(f"[green]requeued[/green] analysis job {job_id}")


# ---------------------------------------------------------------------
# delivery queue sub-app (production Batch 3)
# ---------------------------------------------------------------------

delivery_app = typer.Typer(name="delivery", help="Durable alert-delivery queue")
app.add_typer(delivery_app, name="delivery")


@delivery_app.command("worker")
def delivery_worker() -> None:
    """Run the persistent Telegram/email delivery worker in the foreground."""
    import logging
    from tradingagents.delivery.worker import main

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    main()


@delivery_app.command("status")
def delivery_status() -> None:
    """Show outbox state counts and the ten most recent delivery intents."""
    conn = _conn()
    counts = list(conn.execute(
        "SELECT state, COUNT(*) AS n FROM delivery_queue GROUP BY state "
        "ORDER BY state"
    ))
    if counts:
        for row in counts:
            console.print(f"{row['state']:<8}: {row['n']}")
    else:
        console.print("(delivery queue is empty)")

    rows = list(conn.execute(
        "SELECT delivery_job_id, brief_id, channel, mode, state, "
        "attempt_count, max_attempts, available_ts, last_error "
        "FROM delivery_queue ORDER BY delivery_job_id DESC LIMIT 10"
    ))
    if not rows:
        return
    table = Table("id", "brief", "channel", "mode", "state", "attempts", "ready", "error")
    for row in rows:
        table.add_row(
            str(row["delivery_job_id"]),
            row["brief_id"][:8],
            row["channel"],
            row["mode"],
            row["state"],
            f"{row['attempt_count']}/{row['max_attempts']}",
            (row["available_ts"] or "")[:19],
            (row["last_error"] or "")[:40],
        )
    console.print(table)


@delivery_app.command("retry")
def delivery_retry(delivery_job_id: int) -> None:
    """Give one dead delivery intent one additional attempt."""
    from tradingagents.delivery import queue_store

    if not queue_store.retry_dead(_conn(), delivery_job_id=delivery_job_id):
        raise typer.BadParameter(
            f"delivery job {delivery_job_id} is not in dead state"
        )
    console.print(f"[green]requeued[/green] delivery job {delivery_job_id}")


# ---------------------------------------------------------------------
# F5: morning-digest + digest sub-apps
# ---------------------------------------------------------------------
from cli.morning import morning_app, digest_app  # noqa: E402
app.add_typer(morning_app, name="morning-digest")
app.add_typer(digest_app, name="digest")

from cli.action_handler import action_handler_app  # noqa: E402
app.add_typer(action_handler_app, name="action-handler")

from cli.alert import alert_app  # noqa: E402
app.add_typer(alert_app, name="alert")
