#!/usr/bin/env python
"""Focused production-readiness gate reading the shared operational evidence.

Reads the same snapshot as the dashboard Operations tab
(tradingagents.dashboard.panels.operations.fetch_operations_snapshot) plus
runtime probes that require live subprocess interaction (old services stopped,
Redis owned/configured).

Amendment A: API classification spend is checked via llm_calls (not just the
costs table).  ``count_api_classification_calls`` lives in the operations panel
module (tradingagents.dashboard.panels.operations) so this gate reads the
shared evidence layer; it is imported from there.

Amendment B: The deferred_retry_bounded check also fails when orphaned events
are present; detail string includes orphaned count and oldest pending age.

Amendment C: In ``preflight`` mode, ``llm_calls_present`` and ``sources_fresh``
checks are skipped (fresh stack hasn't produced evidence yet) — marked pass
with a detail note.  All other checks run in both modes.

Check names (stable, runbook-referenced):
  old_services_stopped, redis_owned_and_configured, sources_fresh,
  deferred_retry_bounded, llm_calls_present, llm_failures_bounded,
  no_unexpected_api_classification_spend, delivery_groups_bounded
"""

from __future__ import annotations

try:
    from scripts._repo_bootstrap import ensure_repo_root_on_path
except ModuleNotFoundError:
    from _repo_bootstrap import ensure_repo_root_on_path

ensure_repo_root_on_path()

import argparse
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tradingagents.dashboard.panels.operations import (
    count_api_classification_calls,
    fetch_operations_snapshot,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.persistence.db import connect

# Repo root: two levels above this file (scripts/ → repo root)
_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Legacy unit discovery (derives the list from ops/systemd/ at import time)
# ---------------------------------------------------------------------------

def legacy_unit_names() -> list[str]:
    """Return sorted basenames of all legacy systemd units in ops/systemd/.

    Globs ``ops/systemd/*.service`` and ``ops/systemd/*.timer`` relative to
    the repo root and returns the sorted basenames, EXCLUDING
    ``iic-forge-compose.service`` (the new Compose supervisor — allowed to be
    active).  ``redis-server.service`` is intentionally kept: it is a legacy
    placeholder that must be disabled on the production host.
    """
    systemd_dir = _REPO_ROOT / "ops" / "systemd"
    units = sorted(
        p.name
        for p in systemd_dir.glob("*.service")
        if p.name != "iic-forge-compose.service"
    ) + sorted(
        p.name
        for p in systemd_dir.glob("*.timer")
    )
    return units


# ---------------------------------------------------------------------------
# Default runtime probes (injectable for tests)
# ---------------------------------------------------------------------------

class _SystemctlMissing(Exception):
    """Raised when the systemctl binary is not found on the PATH."""


def default_old_service_checker() -> list[str]:
    """Return names of old systemd units that are still active.

    Derives the unit list from ``legacy_unit_names()`` (globbed from
    ops/systemd/) so new legacy units are automatically included.
    An empty list means all old units are confirmed stopped.

    Raises ``_SystemctlMissing`` when the ``systemctl`` binary is absent
    (e.g. inside a container). The caller (``evaluate()``) converts this into
    a FAILED check so the gate cannot vacuously pass in a container that
    cannot actually probe the host services.

    Only ``FileNotFoundError`` (binary missing) propagates; other subprocess
    errors (nonzero exit, CalledProcessError) mean the unit is inactive and
    are treated as such.
    """
    active: list[str] = []
    for name in legacy_unit_names():
        try:
            out = subprocess.check_output(
                ["systemctl", "is-active", name],
                stderr=subprocess.STDOUT,
                timeout=5,
            ).decode().strip()
        except FileNotFoundError:
            # systemctl binary is missing — cannot probe; re-raise so evaluate()
            # can mark the check as FAILED rather than vacuously passing.
            raise _SystemctlMissing(
                "systemctl binary not found; cannot probe old service state"
            )
        except Exception:
            # Nonzero exit (unit inactive/not-found/unknown) — not active.
            out = "inactive"
        if out == "active":
            active.append(name)
    return active


def default_redis_checker() -> dict[str, Any]:
    """Probe Compose-owned Redis: ping + appendonly config.

    Uses ``--project-directory <repo_root>`` so the gate works when invoked
    from any CWD.  Returns a dict with at least ``ok`` (bool) and
    ``appendonly`` (str).
    """
    project_dir = str(_REPO_ROOT)
    try:
        ping = subprocess.check_output(
            [
                "docker", "compose",
                "--project-directory", project_dir,
                "exec", "-T", "redis", "redis-cli", "ping",
            ],
            stderr=subprocess.STDOUT,
            timeout=10,
        ).decode()
        appendonly = subprocess.check_output(
            [
                "docker", "compose",
                "--project-directory", project_dir,
                "exec", "-T", "redis",
                "redis-cli", "CONFIG", "GET", "appendonly",
            ],
            stderr=subprocess.STDOUT,
            timeout=10,
        ).decode()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {
        "ok": "PONG" in ping,
        "appendonly": "yes" if "yes" in appendonly else "no",
    }


# ---------------------------------------------------------------------------
# Core evaluation function (pure given injected checkers — unit-testable)
# ---------------------------------------------------------------------------

def _age_ok(value: float | None, threshold: int) -> bool:
    return value is not None and value <= threshold


def evaluate(
    conn: sqlite3.Connection,
    *,
    now_ts: str,
    enabled_sources: list[str],
    source_stale_after_seconds: int,
    deferred_pending_max: int,
    failed_delivery_group_max: int,
    allow_api_classification_spend: bool,
    old_service_checker: Callable[[], list[str]],
    redis_checker: Callable[[], dict[str, Any]],
    mode: str = "soak",
    skip_host_probes: bool = False,
) -> dict[str, Any]:
    """Evaluate production-readiness checks over the shared evidence snapshot.

    Parameters
    ----------
    conn:
        Open SQLite connection (WAL mode recommended).
    now_ts:
        ISO 8601 UTC timestamp string (e.g. ``2026-06-12T10:00:00+00:00``).
        Threads through to the snapshot and age calculations.
    enabled_sources:
        List of source names expected to be fresh (from config).
    source_stale_after_seconds:
        Max acceptable last_poll_age_seconds per source.
    deferred_pending_max:
        Max acceptable pending deferred-retry rows (0 = none expected).
    failed_delivery_group_max:
        Max acceptable failed delivery groups (0 = none expected).
    allow_api_classification_spend:
        When False, any API-provider classification call fails the gate.
    old_service_checker:
        Callable returning list of still-active old service names.
    redis_checker:
        Callable returning dict with at least ``ok`` (bool) and
        ``appendonly`` (str "yes"/"no").
    mode:
        "soak" (default) — all checks run.
        "preflight" — ``llm_calls_present`` and ``sources_fresh`` are skipped
        (fresh stack hasn't produced evidence yet).
    skip_host_probes:
        When True, ``old_services_stopped`` and ``redis_owned_and_configured``
        are marked pass with an explanatory detail string rather than running
        the default checkers.  Use this flag when the gate runs inside a
        container that has no access to the host's systemctl or docker CLI.
        Without this flag, a missing systemctl binary causes
        ``old_services_stopped`` to FAIL LOUD (not vacuously pass) so the
        gate is honest about what it could not check.

    Returns
    -------
    dict with keys: ``generated_ts``, ``checks``, ``pass``, ``snapshot``.
    ``checks`` maps check name → ``{"pass": bool, "detail": str}``.
    ``pass`` is True only when all checks pass.
    """
    snap = fetch_operations_snapshot(conn, now_ts=now_ts)
    checks: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 1. Old services stopped
    # ------------------------------------------------------------------
    _skip_detail = (
        "skipped: host probes unavailable in this execution context "
        "(run the gate on the host for these)"
    )
    if skip_host_probes:
        checks["old_services_stopped"] = {"pass": True, "detail": _skip_detail}
    else:
        try:
            old_active = old_service_checker()
            checks["old_services_stopped"] = {
                "pass": old_active == [],
                "detail": f"active old services: {old_active or 'none'}",
            }
        except _SystemctlMissing as exc:
            checks["old_services_stopped"] = {
                "pass": False,
                "detail": (
                    f"FAILED: systemctl binary not found — cannot verify old services "
                    f"are stopped; use --skip-host-probes when running inside a container. "
                    f"({exc})"
                ),
            }

    # ------------------------------------------------------------------
    # 2. Redis owned and configured
    # ------------------------------------------------------------------
    if skip_host_probes:
        checks["redis_owned_and_configured"] = {"pass": True, "detail": _skip_detail}
    else:
        redis = redis_checker()
        checks["redis_owned_and_configured"] = {
            "pass": bool(redis.get("ok")) and redis.get("appendonly") == "yes",
            "detail": json.dumps(redis, sort_keys=True),
        }

    # ------------------------------------------------------------------
    # 3. Sources fresh (skipped in preflight mode)
    # ------------------------------------------------------------------
    if mode == "preflight":
        checks["sources_fresh"] = {
            "pass": True,
            "detail": "skipped in preflight mode",
        }
    else:
        stale = []
        for source in enabled_sources:
            row = snap["sources"].get(source)
            if row is None:
                stale.append(f"{source}:missing")
                continue
            if row["consecutive_failures"] > 0:
                stale.append(f"{source}:failures={row['consecutive_failures']}")
            if not _age_ok(row.get("last_poll_age_seconds"), source_stale_after_seconds):
                stale.append(f"{source}:last_poll_age={row.get('last_poll_age_seconds')}")
        checks["sources_fresh"] = {
            "pass": stale == [],
            "detail": f"stale sources: {stale or 'none'}",
        }

    # ------------------------------------------------------------------
    # 4. Deferred retry bounded (Amendment B: also checks orphaned events)
    # ------------------------------------------------------------------
    deferred = snap["deferred_salience"]
    pending = int(deferred.get("pending", 0))
    orphaned = int(deferred.get("orphaned_events", 0))
    oldest_age = deferred.get("oldest_pending_age_seconds")
    deferred_pass = pending <= deferred_pending_max and orphaned == 0
    checks["deferred_retry_bounded"] = {
        "pass": deferred_pass,
        "detail": (
            f"pending={pending} max={deferred_pending_max} "
            f"orphaned={orphaned} "
            f"oldest_pending_age_seconds={oldest_age} "
            f"states={deferred}"
        ),
    }

    # ------------------------------------------------------------------
    # 5. LLM calls present (skipped in preflight mode)
    # ------------------------------------------------------------------
    llm = snap["llm_calls"]
    if mode == "preflight":
        checks["llm_calls_present"] = {
            "pass": True,
            "detail": "skipped in preflight mode",
        }
    else:
        classification_calls = (
            llm.get("triage_salience", {}).get("total", 0)
            + llm.get("alert_gate", {}).get("total", 0)
        )
        checks["llm_calls_present"] = {
            "pass": classification_calls > 0,
            "detail": f"classification_calls={classification_calls}",
        }

    # ------------------------------------------------------------------
    # 6. LLM failures bounded
    # ------------------------------------------------------------------
    parse_failures = sum(int(v.get("parse_failures") or 0) for v in llm.values())
    transport_failures = sum(int(v.get("transport_failures") or 0) for v in llm.values())
    checks["llm_failures_bounded"] = {
        "pass": parse_failures == 0 and transport_failures == 0,
        "detail": f"parse_failures={parse_failures} transport_failures={transport_failures}",
    }

    # ------------------------------------------------------------------
    # 7. No unexpected API classification spend (Amendment A)
    #    Fails when allow_api_classification_spend is False AND:
    #      - api_spend > 0 (costs table), OR
    #      - any llm_calls row with role in classification roles AND
    #        provider NOT in ('local')
    # ------------------------------------------------------------------
    api_spend = float(snap["costs"].get("api_spend", 0.0))
    api_classification_call_count = count_api_classification_calls(conn, now_ts=now_ts)
    spend_clean = allow_api_classification_spend or (
        api_spend == 0.0 and api_classification_call_count == 0
    )
    checks["no_unexpected_api_classification_spend"] = {
        "pass": spend_clean,
        "detail": (
            f"api_spend={api_spend:.6f} "
            f"api_classification_calls={api_classification_call_count}"
        ),
    }

    # ------------------------------------------------------------------
    # 8. Delivery groups bounded — uses failed_total (unbounded count)
    # ------------------------------------------------------------------
    failed_total = snap["delivery_groups"]["failed_total"]
    checks["delivery_groups_bounded"] = {
        "pass": failed_total <= failed_delivery_group_max,
        "detail": f"failed_groups={failed_total} max={failed_delivery_group_max}",
    }

    return {
        "generated_ts": now_ts,
        "checks": checks,
        "pass": all(check["pass"] for check in checks.values()),
        "snapshot": snap,
    }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="focused_soak_gate",
        description="Focused Soak Gate — production-readiness check over shared operational evidence.",
    )
    parser.add_argument(
        "--mode",
        choices=("preflight", "soak"),
        default="soak",
        help=(
            "preflight: skips sources_fresh and llm_calls_present checks "
            "(fresh stack has no evidence yet). "
            "soak: all checks run. Default: soak."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output full JSON report (machine-readable).",
    )
    parser.add_argument(
        "--skip-host-probes",
        action="store_true",
        default=False,
        help=(
            "Skip old_services_stopped and redis_owned_and_configured host probes "
            "(mark them pass with an explanatory note). Use when running inside a "
            "container that has no access to the host's systemctl or docker CLI. "
            "Without this flag a missing systemctl binary causes old_services_stopped "
            "to FAIL LOUD rather than vacuously pass."
        ),
    )
    args = parser.parse_args()

    conn = connect(DEFAULT_CONFIG["iic_db_path"])
    report = evaluate(
        conn,
        now_ts=datetime.now(timezone.utc).isoformat(),
        enabled_sources=[
            k for k, v in DEFAULT_CONFIG["sensing_adapters_enabled"].items() if v
        ],
        source_stale_after_seconds=int(
            DEFAULT_CONFIG.get("source_stale_after_seconds", 1800)
        ),
        deferred_pending_max=int(
            DEFAULT_CONFIG.get("deferred_retry_max_pending", 0)
        ),
        failed_delivery_group_max=int(
            DEFAULT_CONFIG.get("delivery_failed_group_max", 0)
        ),
        allow_api_classification_spend=bool(
            DEFAULT_CONFIG.get("allow_api_classification_spend", False)
        ),
        old_service_checker=default_old_service_checker,
        redis_checker=default_redis_checker,
        mode=args.mode,
        skip_host_probes=args.skip_host_probes,
    )

    if args.json:
        # JSON embeds the full snapshot; failed-groups list is capped at 50 by
        # the shared layer; output size scales with failure count.
        print(json.dumps(report, indent=2, default=str))
    else:
        mark = "PASS" if report["pass"] else "FAIL"
        print(f"# Focused Soak Gate [{args.mode}] - {mark}")
        for name, check in report["checks"].items():
            status = "PASS" if check["pass"] else "FAIL"
            print(f"- {name}: {status} - {check['detail']}")

    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
