"""IIC-FORGE operational CLI.

Sub-apps:
  - watchlist : manage the curated watchlist (add / list / remove)
  - sense     : sensing-related ops (seed tickers, status, force sweep)

Wired into the main `tradingagents` CLI by ``cli/main.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
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

    db_path = (
        os.environ.get("TRADINGAGENTS_IIC_DB_PATH") or DEFAULT_CONFIG["iic_db_path"]
    )
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
    rows = list(
        conn.execute(
            "SELECT ticker, added_ts, last_briefed, ttl_until, tags "
            "FROM watchlist ORDER BY ticker"
        )
    )
    if not rows:
        console.print("(watchlist is empty)")
        return
    t = Table("ticker", "added", "last_briefed", "ttl_until", "tags")
    for r in rows:
        tags = ", ".join(json.loads(r["tags"]) if r["tags"] else [])
        t.add_row(
            r["ticker"],
            r["added_ts"] or "",
            r["last_briefed"] or "",
            r["ttl_until"] or "",
            tags,
        )
    console.print(t)


@watchlist_app.command("remove")
def watchlist_remove(ticker: str) -> None:
    """Remove a ticker from the watchlist (works for user or auto rows)."""
    conn = _conn()
    n = conn.execute(
        "DELETE FROM watchlist WHERE ticker = ?", (ticker.upper(),)
    ).rowcount
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
    no_polygon: bool = typer.Option(
        False, "--no-polygon", help="Skip Polygon equity seed (crypto only)"
    ),
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
        console.print(
            f"crypto: {result['crypto']} rows; polygon: {result['polygon']} rows"
        )


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
    """Run the promoter loop in the foreground (Compose supervises production)."""
    from tradingagents.orchestrator.promoter import main
    from tradingagents.ops.heartbeat import ServiceHeartbeat
    from tradingagents.ops.logging import configure_logging

    configure_logging("promoter")
    with ServiceHeartbeat(
        DEFAULT_CONFIG["iic_db_path"],
        "promoter",
        interval_seconds=DEFAULT_CONFIG["operator_heartbeat_interval_seconds"],
    ):
        main()


@orch_app.command("worker")
def orchestrator_worker() -> None:
    """Run the worker loop in the foreground (Compose supervises production)."""
    from tradingagents.orchestrator.worker import main
    from tradingagents.ops.heartbeat import ServiceHeartbeat
    from tradingagents.ops.logging import configure_logging

    configure_logging("analysis-worker")
    with ServiceHeartbeat(
        DEFAULT_CONFIG["iic_db_path"],
        "analysis-worker",
        interval_seconds=DEFAULT_CONFIG["operator_heartbeat_interval_seconds"],
    ):
        main()


@orch_app.command("status")
def orchestrator_status() -> None:
    """Quick view of queue depth + recent jobs + today's spend."""
    from tradingagents.orchestrator import queue_store
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.llm_clients.daily_budget import (
        beijing_budget_date,
        daily_budget_total,
    )
    from tradingagents.ops.logging import redact

    conn = _conn()
    pending = queue_store.pending_count(conn)
    today_enqueued = queue_store.daily_enqueue_count(conn)
    budget_date = beijing_budget_date(
        timezone_name=DEFAULT_CONFIG["daily_budget_timezone"]
    )
    today_cost = daily_budget_total(conn, budget_date=budget_date)

    console.print(f"pending (queued+running): [bold]{pending}[/bold]")
    console.print(f"enqueued today          : {today_enqueued}")
    console.print(
        f"combined LLM spend {budget_date} (USD): ${today_cost:.4f} / "
        f"${DEFAULT_CONFIG['daily_budget_usd']:.2f}"
    )

    rows = list(
        conn.execute(
            "SELECT job_id, job_type, state, enqueued_ts, finished_ts, "
            "brief_id, cost_usd, error, error_category, worker_pid, operator_note "
            "FROM queue_jobs ORDER BY job_id DESC LIMIT 10"
        )
    )
    if not rows:
        console.print("(no jobs)")
        return
    t = Table(
        "id",
        "type",
        "state",
        "pid",
        "category",
        "enqueued",
        "finished",
        "brief",
        "$",
        "err/note",
    )
    for r in rows:
        t.add_row(
            str(r["job_id"]),
            r["job_type"],
            r["state"],
            str(r["worker_pid"] or ""),
            (r["error_category"] or "")[:20],
            (r["enqueued_ts"] or "")[:19],
            (r["finished_ts"] or "")[:19],
            (r["brief_id"] or "")[:8],
            f"{(r['cost_usd'] or 0.0):.4f}",
            redact(r["operator_note"] or r["error"] or "")[:40],
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
    if not queue_store.retry_error_job(_conn(), job_id=job_id, operator_note=note):
        raise typer.BadParameter(f"job {job_id} is not in error or blocked state")
    console.print(f"[green]requeued[/green] analysis job {job_id}")


@orch_app.command("inspect")
def orchestrator_inspect(job_id: int) -> None:
    """Show one analysis job without exposing its untrusted payload."""
    from tradingagents.orchestrator import queue_store

    job = queue_store.inspect_job(_conn(), job_id=job_id)
    if job is None:
        raise typer.BadParameter(f"analysis job {job_id} does not exist")
    console.print_json(data=job)


@orch_app.command("cancel")
def orchestrator_cancel(
    job_id: int,
    note: str = typer.Option(..., "--note", help="Why inactive work is cancelled."),
) -> None:
    """Cancel queued, blocked, or exhausted analysis work; never kill a runner."""
    from tradingagents.orchestrator import queue_store

    if not note.strip():
        raise typer.BadParameter("--note must not be empty")
    if not queue_store.cancel_job(_conn(), job_id=job_id, operator_note=note):
        raise typer.BadParameter(
            f"analysis job {job_id} is not queued, blocked, or error"
        )
    console.print(f"[yellow]cancelled[/yellow] analysis job {job_id}")


@orch_app.command("drain")
def orchestrator_drain(
    timeout_seconds: int = typer.Option(300, "--timeout-seconds", min=1),
    poll_seconds: float = typer.Option(2.0, "--poll-seconds", min=0.1),
) -> None:
    """Wait until active analysis work is empty after producers are stopped."""
    import time
    from tradingagents.orchestrator import queue_store

    deadline = time.monotonic() + timeout_seconds
    conn = _conn()
    try:
        while True:
            pending = queue_store.pending_count(conn)
            if pending == 0:
                console.print_json(data={"status": "drained", "pending": 0})
                return
            if time.monotonic() >= deadline:
                console.print_json(data={"status": "timeout", "pending": pending})
                raise typer.Exit(code=1)
            time.sleep(poll_seconds)
    finally:
        conn.close()


# ---------------------------------------------------------------------
# delivery queue sub-app (production Batch 3)
# ---------------------------------------------------------------------

delivery_app = typer.Typer(name="delivery", help="Durable alert-delivery queue")
app.add_typer(delivery_app, name="delivery")


@delivery_app.command("worker")
def delivery_worker() -> None:
    """Run the persistent Telegram/email delivery worker in the foreground."""
    from tradingagents.delivery.worker import main
    from tradingagents.ops.heartbeat import ServiceHeartbeat
    from tradingagents.ops.logging import configure_logging

    configure_logging("delivery-worker")
    with ServiceHeartbeat(
        DEFAULT_CONFIG["iic_db_path"],
        "delivery-worker",
        interval_seconds=DEFAULT_CONFIG["operator_heartbeat_interval_seconds"],
    ):
        main()


@delivery_app.command("status")
def delivery_status() -> None:
    """Show outbox state counts and the ten most recent delivery intents."""
    from tradingagents.ops.logging import redact

    conn = _conn()
    counts = list(
        conn.execute(
            "SELECT state, COUNT(*) AS n FROM delivery_queue GROUP BY state "
            "ORDER BY state"
        )
    )
    if counts:
        for row in counts:
            console.print(f"{row['state']:<8}: {row['n']}")
    else:
        console.print("(delivery queue is empty)")

    rows = list(
        conn.execute(
            "SELECT delivery_job_id, brief_id, channel, mode, state, "
            "attempt_count, max_attempts, available_ts, error_category, last_error "
            "FROM delivery_queue ORDER BY delivery_job_id DESC LIMIT 10"
        )
    )
    if not rows:
        return
    table = Table(
        "id",
        "brief",
        "channel",
        "mode",
        "state",
        "attempts",
        "ready",
        "category",
        "error",
    )
    for row in rows:
        table.add_row(
            str(row["delivery_job_id"]),
            row["brief_id"][:8],
            row["channel"],
            row["mode"],
            row["state"],
            f"{row['attempt_count']}/{row['max_attempts']}",
            (row["available_ts"] or "")[:19],
            (row["error_category"] or "")[:28],
            redact(row["last_error"] or "")[:40],
        )
    console.print(table)


@delivery_app.command("retry")
def delivery_retry(
    delivery_job_id: int,
    note: str = typer.Option(
        ..., "--note", help="Why the exhausted job is safe to retry."
    ),
) -> None:
    """Give one dead delivery intent one additional attempt."""
    from tradingagents.delivery import queue_store

    if not note.strip():
        raise typer.BadParameter("--note must not be empty")
    if not queue_store.retry_dead(
        _conn(), delivery_job_id=delivery_job_id, operator_note=note
    ):
        raise typer.BadParameter(f"delivery job {delivery_job_id} is not in dead state")
    console.print(f"[green]requeued[/green] delivery job {delivery_job_id}")


@delivery_app.command("requeue")
def delivery_requeue(
    delivery_job_id: int,
    note: str = typer.Option(
        ..., "--note", help="Correction made before replaying the blocked job."
    ),
) -> None:
    """Requeue one blocked delivery after correcting its configuration."""
    from tradingagents.delivery import queue_store

    if not note.strip():
        raise typer.BadParameter("--note must not be empty")
    if not queue_store.requeue_blocked(
        _conn(), delivery_job_id=delivery_job_id, operator_note=note
    ):
        raise typer.BadParameter(
            f"delivery job {delivery_job_id} is not in blocked state"
        )
    console.print(f"[green]requeued[/green] blocked delivery job {delivery_job_id}")


@delivery_app.command("cancel")
def delivery_cancel(
    delivery_job_id: int,
    note: str = typer.Option(
        ..., "--note", help="Why this delivery is being cancelled."
    ),
) -> None:
    """Cancel a queued, blocked, or dead intent; active/sent work is refused."""
    from tradingagents.delivery import queue_store

    if not note.strip():
        raise typer.BadParameter("--note must not be empty")
    if not queue_store.cancel_delivery(
        _conn(), delivery_job_id=delivery_job_id, operator_note=note
    ):
        raise typer.BadParameter(
            f"delivery job {delivery_job_id} is not queued, blocked, or dead"
        )
    console.print(f"[yellow]cancelled[/yellow] delivery job {delivery_job_id}")


@delivery_app.command("inspect")
def delivery_inspect(delivery_job_id: int) -> None:
    """Show one delivery intent and its append-only lifecycle audit events."""
    from tradingagents.delivery import queue_store

    job, events = queue_store.inspect_delivery(_conn(), delivery_job_id=delivery_job_id)
    if job is None:
        raise typer.BadParameter(f"delivery job {delivery_job_id} does not exist")
    console.print_json(data={"job": job, "events": events})


# ---------------------------------------------------------------------
# production runtime sub-app
# ---------------------------------------------------------------------

runtime_app = typer.Typer(
    name="runtime", help="Docker Compose initialization and health controls"
)
app.add_typer(runtime_app, name="runtime")


@runtime_app.command("init")
def runtime_init(
    require_production_config: bool = typer.Option(
        False,
        "--require-production-config",
        help="Refuse startup unless the complete private-production contract is set",
    ),
) -> None:
    """Create private data paths, migrate SQLite, and verify integrity."""
    from tradingagents.runtime import initialize_runtime

    result = initialize_runtime(
        DEFAULT_CONFIG,
        require_production_config=require_production_config,
    )
    console.print_json(json.dumps(result))


@runtime_app.command("health")
def runtime_health(
    database: bool = typer.Option(True, "--database/--no-database"),
    redis: bool = typer.Option(True, "--redis/--no-redis"),
) -> None:
    """Exit non-zero unless the selected production dependencies are healthy."""
    from tradingagents.runtime import check_database, check_redis

    if not database and not redis:
        raise typer.BadParameter("at least one health check must be enabled")
    result = {}
    if database:
        result["database"] = check_database(DEFAULT_CONFIG)
    if redis:
        result["redis"] = check_redis(DEFAULT_CONFIG)
    console.print_json(json.dumps(result))


@runtime_app.command("run")
def runtime_run(service: str) -> None:
    """Run one supported Compose service in the foreground."""
    from tradingagents.runtime import run_named_service
    from tradingagents.ops.heartbeat import ServiceHeartbeat
    from tradingagents.ops.logging import configure_logging

    configure_logging(service)
    with ServiceHeartbeat(
        DEFAULT_CONFIG["iic_db_path"],
        service,
        interval_seconds=DEFAULT_CONFIG["operator_heartbeat_interval_seconds"],
    ):
        run_named_service(service)


# ---------------------------------------------------------------------
# Batch 9 operator observability and administrative controls
# ---------------------------------------------------------------------

operator_app = typer.Typer(
    name="operator", help="Private production status, preflight, and recovery controls"
)
app.add_typer(operator_app, name="operator")


def _backup_root() -> Path:
    import os

    return Path(os.environ.get("IIC_BACKUP_DIR", "./backups")).expanduser().resolve()


def _operator_config() -> dict:
    """Refresh path overrides for repeated in-process CLI invocations/tests."""
    import os

    config = dict(DEFAULT_CONFIG)
    config["iic_db_path"] = os.environ.get(
        "TRADINGAGENTS_IIC_DB_PATH", config["iic_db_path"]
    )
    config["iic_data_dir"] = os.environ.get(
        "TRADINGAGENTS_IIC_DATA_DIR", config["iic_data_dir"]
    )
    config["sensing_redis_url"] = os.environ.get(
        "TRADINGAGENTS_SENSING_REDIS_URL", config["sensing_redis_url"]
    )
    return config


@operator_app.command("status")
def operator_status(
    full_database_check: bool = typer.Option(False, "--full-database-check"),
    redis: bool = typer.Option(True, "--redis/--no-redis"),
) -> None:
    """Emit a bounded JSON snapshot; queue payloads and secrets are excluded."""
    from tradingagents.ops.status import collect_status

    config = _operator_config()
    conn = _conn()
    try:
        result = collect_status(
            conn,
            config,
            backup_root=_backup_root(),
            full_database_check=full_database_check,
            redis_check=redis,
        )
    finally:
        conn.close()
    console.print_json(data=result)


@operator_app.command("preflight")
def operator_preflight(
    require_production_config: bool = typer.Option(
        False, "--require-production-config"
    ),
    require_all_services: bool = typer.Option(
        False, "--require-all-services/--allow-missing-services"
    ),
) -> None:
    """Run full integrity, dependency, capacity, and production-contract checks."""
    from tradingagents.ops.status import collect_status, health_issues
    from tradingagents.runtime import validate_production_environment

    config = _operator_config()
    config_errors = (
        validate_production_environment(config)
        if require_production_config
        else []
    )
    conn = _conn()
    try:
        snapshot = collect_status(
            conn,
            config,
            backup_root=_backup_root(),
            full_database_check=True,
            redis_check=True,
        )
    finally:
        conn.close()
    issues = health_issues(snapshot, require_all_services=require_all_services)
    result = {
        "status": "ok" if not config_errors and not issues else "failed",
        "configuration_errors": config_errors,
        "health_issues": issues,
        "snapshot": snapshot,
    }
    console.print_json(data=result)
    if result["status"] != "ok":
        raise typer.Exit(code=1)


@operator_app.command("monitor")
def operator_monitor() -> None:
    """Run the durable alerting monitor in the foreground."""
    from tradingagents.ops.heartbeat import ServiceHeartbeat
    from tradingagents.ops.logging import configure_logging
    from tradingagents.ops.monitor import main

    configure_logging("operator-monitor")
    with ServiceHeartbeat(
        DEFAULT_CONFIG["iic_db_path"],
        "operator-monitor",
        interval_seconds=DEFAULT_CONFIG["operator_heartbeat_interval_seconds"],
    ):
        main()


@operator_app.command("alerts")
def operator_alerts(open_only: bool = typer.Option(True, "--open/--all")) -> None:
    """List durable operational alert metadata and occurrence counts."""
    conn = _conn()
    where = "WHERE state='open'" if open_only else ""
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT alert_id, dedup_key, category, severity, summary, state, "
            "first_seen_ts, last_seen_ts, occurrence_count, last_delivery_ts, "
            "resolved_ts, operator_note FROM operational_alerts "
            f"{where} ORDER BY last_seen_ts DESC LIMIT 200"
        )
    ]
    conn.close()
    console.print_json(data={"alerts": rows})


@operator_app.command("release-budget")
def operator_release_budget(
    call_id: str,
    note: str = typer.Option(..., "--note"),
    evidence: str = typer.Option(..., "--evidence"),
    confirm: str = typer.Option(..., "--confirm"),
    minimum_age_hours: int = typer.Option(1, "--minimum-age-hours", min=1),
) -> None:
    """Release only an old abandoned reservation; preserve the ledger row."""
    from tradingagents.llm_clients.daily_budget import release_stale_reservation

    conn = _conn()
    try:
        released = release_stale_reservation(
            conn,
            call_id=call_id,
            operator_note=note,
            evidence=evidence,
            confirm=confirm,
            minimum_age_hours=minimum_age_hours,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        conn.close()
    console.print_json(data={"call_id": call_id, "released_usd": released})


@operator_app.command("retention")
def operator_retention(
    apply: bool = typer.Option(False, "--apply/--preview"),
    note: str = typer.Option("", "--note"),
    confirm: str = typer.Option("", "--confirm"),
) -> None:
    """Preview bounded retention; apply only with note and exact confirmation."""
    from tradingagents.ops.retention import apply_retention, retention_candidates

    conn = _conn()
    config = _operator_config()
    try:
        candidates = retention_candidates(
            conn, data_dir=config["iic_data_dir"]
        )
        if not apply:
            console.print_json(data={"status": "preview", **candidates})
            return
        try:
            result = apply_retention(
                conn,
                candidates=candidates,
                operator_note=note,
                confirm=confirm,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        console.print_json(data={"status": "applied", **result})
    finally:
        conn.close()


@operator_app.command("restore-drill")
def operator_restore_drill(
    archive: Path,
    key_file: Path = typer.Option(..., "--key-file"),
    note: str = typer.Option(..., "--note"),
    confirm: str = typer.Option(..., "--confirm"),
) -> None:
    """Actually restore into disposable roots and persist the drill result."""
    from tradingagents.backup import BackupError
    from tradingagents.ops.recovery import run_restore_drill

    conn = _conn()
    try:
        result = run_restore_drill(
            conn,
            archive=archive,
            key_file=key_file,
            operator_note=note,
            confirm=confirm,
        )
    except (BackupError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        conn.close()
    console.print_json(data=result)
    if result["status"] != "passed":
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------
# production backup sub-app (Batch 8)
# ---------------------------------------------------------------------

backup_app = typer.Typer(
    name="backup", help="Encrypted local backup, verification, and restore controls"
)
app.add_typer(backup_app, name="backup")


@backup_app.command("create")
def backup_create(
    data_root: Path = typer.Option(..., "--data-root"),
    redis_root: Path = typer.Option(..., "--redis-root"),
    output_root: Path = typer.Option(..., "--output-root"),
    key_file: Path = typer.Option(..., "--key-file"),
    label: str = typer.Option("scheduled", "--label"),
    keep_hourly_hours: int = typer.Option(48, "--keep-hourly-hours"),
    keep_daily_days: int = typer.Option(14, "--keep-daily-days"),
    keep_weekly_weeks: int = typer.Option(8, "--keep-weekly-weeks"),
    prune: bool = typer.Option(True, "--prune/--no-prune"),
) -> None:
    """Create and immediately verify one stopped-stack local snapshot."""
    from tradingagents.backup import BackupError, create_backup

    try:
        result = create_backup(
            data_root=data_root,
            redis_root=redis_root,
            output_root=output_root,
            key_file=key_file,
            label=label,
            keep_hourly_hours=keep_hourly_hours,
            keep_daily_days=keep_daily_days,
            keep_weekly_weeks=keep_weekly_weeks,
            apply_retention=prune,
        )
    except BackupError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print_json(json.dumps(result))


@backup_app.command("verify")
def backup_verify(
    archive: Path,
    key_file: Path = typer.Option(..., "--key-file"),
) -> None:
    """Authenticate the archive, every member hash, and the SQLite payload."""
    from tradingagents.backup import BackupError, verify_backup

    try:
        result = verify_backup(archive, key_file=key_file)
    except BackupError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print_json(json.dumps(result))


@backup_app.command("status")
def backup_show_status(
    output_root: Path = typer.Option(..., "--output-root"),
    max_age_minutes: int = typer.Option(60, "--max-age-minutes"),
) -> None:
    """Fail unless the latest verified encrypted snapshot is current."""
    from tradingagents.backup import BackupError, backup_status

    try:
        result = backup_status(
            output_root, max_age_minutes=max_age_minutes
        )
    except BackupError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print_json(json.dumps(result))


@backup_app.command("restore")
def backup_restore(
    archive: Path,
    data_root: Path = typer.Option(..., "--data-root"),
    redis_root: Path = typer.Option(..., "--redis-root"),
    key_file: Path = typer.Option(..., "--key-file"),
    confirm: str = typer.Option(..., "--confirm"),
) -> None:
    """Restore a verified snapshot into stopped data and Redis volumes."""
    from tradingagents.backup import BackupError, restore_backup

    try:
        result = restore_backup(
            archive,
            key_file=key_file,
            data_root=data_root,
            redis_root=redis_root,
            confirm=confirm,
        )
    except BackupError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print_json(json.dumps(result))


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
