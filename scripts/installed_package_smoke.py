#!/usr/bin/env python3
"""Smoke-test an installed IIC-Forge artifact away from its source checkout."""

from __future__ import annotations

import argparse
import tempfile
from importlib.resources import files
from pathlib import Path

import cli
import tradingagents
from tradingagents.delivery.render import _plain_env as delivery_templates
from tradingagents.persistence.db import connect, schema_tables
from tradingagents.personas.resolver import load_packaged_persona
from tradingagents.runtime import check_database
from tradingagents.secretary.service import _env as secretary_templates
from tradingagents.sensing.seed_tickers import seed_crypto


def _assert_outside_source(module_path: str | None, forbidden_root: Path) -> None:
    if module_path is None:
        raise RuntimeError("installed package has no filesystem origin")
    resolved = Path(module_path).resolve()
    root = forbidden_root.resolve()
    if resolved == root or root in resolved.parents:
        raise RuntimeError(f"module unexpectedly imported from source tree: {resolved}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--forbid-root", required=True, type=Path)
    args = parser.parse_args()

    _assert_outside_source(tradingagents.__file__, args.forbid_root)
    _assert_outside_source(cli.__file__, args.forbid_root)

    welcome = files("cli").joinpath("static", "welcome.txt").read_text("utf-8")
    if not welcome.strip():
        raise RuntimeError("packaged CLI welcome text is empty")

    persona = load_packaged_persona("balanced")
    if persona is None or persona.id != "balanced":
        raise RuntimeError("packaged balanced persona did not load")

    secretary_templates.get_template("deep_dive.j2")
    delivery_templates.get_template("telegram/morning_digest.j2")
    delivery_templates.get_template("email/deep_dive.j2")
    delivery_templates.get_template("email/event_alert.j2")

    with tempfile.TemporaryDirectory(prefix="iic-forge-installed-smoke-") as tmp:
        database_path = str(Path(tmp) / "iic.db")
        conn = connect(database_path)
        try:
            present = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            missing_tables = schema_tables() - present
            if missing_tables:
                raise RuntimeError(f"packaged schema missing tables: {missing_tables}")
            if seed_crypto(conn) < 1:
                raise RuntimeError("packaged crypto universe produced no rows")
        finally:
            conn.close()
        health = check_database({"iic_db_path": database_path})
        if health["status"] != "ok":
            raise RuntimeError(f"packaged runtime database health failed: {health}")

    print("installed-package smoke test passed")


if __name__ == "__main__":
    main()
