#!/usr/bin/env python3
"""Verify that built IIC-Forge distributions contain every runtime resource."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path


REQUIRED_PACKAGE_FILES = {
    "cli/static/welcome.txt",
    "tradingagents/backup/__init__.py",
    "tradingagents/backup/archive.py",
    "tradingagents/delivery/templates/cli/deep_dive.j2",
    "tradingagents/delivery/templates/cli/event_alert.j2",
    "tradingagents/delivery/templates/cli/event_alert_light.j2",
    "tradingagents/delivery/templates/cli/morning_digest.j2",
    "tradingagents/delivery/templates/email/deep_dive.j2",
    "tradingagents/delivery/templates/email/event_alert.j2",
    "tradingagents/delivery/templates/email/event_alert_light.j2",
    "tradingagents/delivery/templates/email/morning_digest.j2",
    "tradingagents/delivery/templates/telegram/deep_dive.j2",
    "tradingagents/delivery/templates/telegram/event_alert.j2",
    "tradingagents/delivery/templates/telegram/event_alert_light.j2",
    "tradingagents/delivery/templates/telegram/morning_digest.j2",
    "tradingagents/persistence/migrations/0001_baseline.sql",
    "tradingagents/persistence/migrations/0002_queue_lifecycle.sql",
    "tradingagents/persistence/migrations/0003_analysis_worker_process.sql",
    "tradingagents/persistence/migrations/0004_delivery_outbox_controls.sql",
    "tradingagents/persistence/migrations/0005_quality_security_budget.sql",
    "tradingagents/personas/balanced.yaml",
    "tradingagents/personas/macro.yaml",
    "tradingagents/personas/momentum.yaml",
    "tradingagents/personas/value.yaml",
    "tradingagents/secretary/templates/deep_dive.j2",
    "tradingagents/secretary/templates/event_alert.j2",
    "tradingagents/sensing/data/crypto_universe.yaml",
}


def _archive_names(path: Path) -> set[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return set(archive.namelist())
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            names = set(archive.getnames())
        return {name.split("/", 1)[1] for name in names if "/" in name}
    raise ValueError(f"unsupported distribution format: {path}")


def verify(path: Path) -> None:
    names = _archive_names(path)
    missing = sorted(REQUIRED_PACKAGE_FILES - names)
    if missing:
        rendered = "\n  - ".join(missing)
        raise SystemExit(f"{path}: missing runtime resources:\n  - {rendered}")
    print(f"{path}: verified {len(REQUIRED_PACKAGE_FILES)} runtime resources")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("distributions", nargs="+", type=Path)
    args = parser.parse_args()
    for distribution in args.distributions:
        verify(distribution)


if __name__ == "__main__":
    main()
