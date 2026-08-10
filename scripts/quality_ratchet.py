#!/usr/bin/env python3
"""Block new Ruff and mypy debt while allowing the recorded baseline to shrink."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "quality" / "quality-baseline.json"
MYPY_PATTERN = re.compile(
    r"^(?P<path>.*?):(?P<line>\d+)(?::\d+)?: error: "
    r"(?P<message>.*?)(?:\s+\[(?P<code>[^]]+)\])?$"
)


def _relative_path(value: str) -> str:
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def parse_ruff(output: str) -> Counter[tuple[str, str, str]]:
    """Return stable Ruff fingerprints without line numbers."""
    findings: Counter[tuple[str, str, str]] = Counter()
    for item in json.loads(output):
        findings[
            (
                _relative_path(str(item["filename"])),
                str(item["code"]),
                str(item["message"]),
            )
        ] += 1
    return findings


def parse_mypy(output: str) -> Counter[tuple[str, str, str]]:
    """Return stable mypy fingerprints without source positions."""
    findings: Counter[tuple[str, str, str]] = Counter()
    for line in output.splitlines():
        match = MYPY_PATTERN.match(line)
        if match is None:
            continue
        findings[
            (
                _relative_path(match.group("path")),
                match.group("code") or "untyped",
                match.group("message"),
            )
        ] += 1
    return findings


def regressions(
    baseline: Counter[tuple[str, str, str]],
    current: Counter[tuple[str, str, str]],
) -> list[tuple[tuple[str, str, str], int, int]]:
    """Return fingerprints whose current multiplicity exceeds the allowance."""
    return [
        (fingerprint, baseline.get(fingerprint, 0), count)
        for fingerprint, count in sorted(current.items())
        if count > baseline.get(fingerprint, 0)
    ]


def _run(command: list[str], *, allowed_codes: Iterable[int]) -> str:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode not in set(allowed_codes):
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"command failed with exit {completed.returncode}: "
            f"{' '.join(command)}\n{detail}"
        )
    return completed.stdout


def _version(executable: str) -> str:
    return _run([executable, "--version"], allowed_codes=(0,)).strip()


def collect(ruff: str, mypy: str) -> tuple[dict[str, str], dict[str, Counter]]:
    versions = {"ruff": _version(ruff), "mypy": _version(mypy)}
    ruff_output = _run(
        [ruff, "check", ".", "--output-format", "json"],
        allowed_codes=(0, 1),
    )
    mypy_output = _run(
        [
            mypy,
            "--ignore-missing-imports",
            "--follow-imports=skip",
            "--show-error-codes",
            "tradingagents",
            "cli",
        ],
        allowed_codes=(0, 1),
    )
    return versions, {"ruff": parse_ruff(ruff_output), "mypy": parse_mypy(mypy_output)}


def _serialize_counter(counter: Counter[tuple[str, str, str]]) -> list[dict[str, object]]:
    return [
        {"path": path, "code": code, "message": message, "count": count}
        for (path, code, message), count in sorted(counter.items())
    ]


def _deserialize_counter(items: list[dict[str, object]]) -> Counter[tuple[str, str, str]]:
    result: Counter[tuple[str, str, str]] = Counter()
    for item in items:
        result[
            (str(item["path"]), str(item["code"]), str(item["message"]))
        ] = int(item["count"])
    return result


def update_baseline(
    path: Path,
    versions: dict[str, str],
    findings: dict[str, Counter],
) -> None:
    payload = {
        "schema_version": 1,
        "policy": "No fingerprint may be added or increase; reductions are allowed.",
        "tools": versions,
        "commands": {
            "ruff": "ruff check . --output-format json",
            "mypy": (
                "mypy --ignore-missing-imports --follow-imports=skip "
                "--show-error-codes tradingagents cli"
            ),
        },
        "findings": {
            name: _serialize_counter(counter) for name, counter in findings.items()
        },
        "totals": {name: sum(counter.values()) for name, counter in findings.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check_baseline(
    path: Path,
    versions: dict[str, str],
    findings: dict[str, Counter],
) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_versions = payload.get("tools") or {}
    if versions != expected_versions:
        print(
            "quality tool version mismatch; update the baseline only in an "
            f"intentional tooling change\nexpected={expected_versions}\nactual={versions}",
            file=sys.stderr,
        )
        return 2

    failed = False
    for name in ("ruff", "mypy"):
        baseline = _deserialize_counter(payload["findings"][name])
        current = findings[name]
        new = regressions(baseline, current)
        print(
            f"{name}: current={sum(current.values())} "
            f"baseline={sum(baseline.values())} "
            f"reduced={max(0, sum(baseline.values()) - sum(current.values()))}"
        )
        for (file_name, code, message), allowed, observed in new:
            failed = True
            print(
                f"NEW {name}: {file_name} [{code}] {message} "
                f"(allowed={allowed}, observed={observed})",
                file=sys.stderr,
            )
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "update"))
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--ruff", default="ruff")
    parser.add_argument("--mypy", default="mypy")
    args = parser.parse_args()

    versions, findings = collect(args.ruff, args.mypy)
    baseline = args.baseline.expanduser().resolve()
    if args.mode == "update":
        update_baseline(baseline, versions, findings)
        print(
            f"updated {baseline}: "
            + ", ".join(
                f"{name}={sum(counter.values())}"
                for name, counter in findings.items()
            )
        )
        return 0
    if not baseline.is_file():
        print(f"quality baseline does not exist: {baseline}", file=sys.stderr)
        return 2
    return check_baseline(baseline, versions, findings)


if __name__ == "__main__":
    raise SystemExit(main())
