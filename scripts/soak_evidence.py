#!/usr/bin/env python3
"""Collect and evaluate bounded evidence from a production-like Compose soak."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Mapping


MEMORY_PATTERN = re.compile(r"^\s*([0-9.]+)\s*([kmgt]?i?b)\s*$", re.IGNORECASE)
EXPECTED_LONG_RUNNING = {
    "scheduler",
    "sense-rss",
    "sense-telegram",
    "sense-polygon",
    "triage",
    "promoter",
    "analysis-worker",
    "delivery-worker",
    "telegram-bot",
    "action-handler",
    "operator-monitor",
    "dashboard",
    "redis",
}


def _run(arguments: list[str]) -> str:
    completed = subprocess.run(arguments, check=False, capture_output=True, text=True)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"command failed ({completed.returncode}): {' '.join(arguments)}\n{detail}")
    return completed.stdout.strip()


def _json_records(output: str) -> list[dict[str, Any]]:
    if not output.strip():
        return []
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return [json.loads(line) for line in output.splitlines() if line.strip()]
    return parsed if isinstance(parsed, list) else [parsed]


def memory_bytes(value: str) -> int:
    match = MEMORY_PATTERN.match(value)
    if match is None:
        raise ValueError(f"unrecognized memory value: {value!r}")
    number = float(match.group(1))
    unit = match.group(2).lower()
    powers = {"b": 0, "kb": 1, "kib": 1, "mb": 2, "mib": 2, "gb": 3, "gib": 3, "tb": 4, "tib": 4}
    base = 1024 if "i" in unit else 1000
    return int(number * (base ** powers[unit]))


def _operator_json() -> dict[str, Any]:
    output = _run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "analysis-worker",
            "iic-forge",
            "forge",
            "operator",
            "status",
            "--full-database-check",
        ]
    )
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end < start:
        raise ValueError("operator status did not emit JSON")
    return json.loads(output[start : end + 1])


def collect_sample() -> dict[str, Any]:
    compose_rows = _json_records(_run(["docker", "compose", "ps", "--format", "json"]))
    by_id = {
        str(row.get("ID") or row.get("Id")): row
        for row in compose_rows
        if row.get("ID") or row.get("Id")
    }
    statistics = _json_records(
        _run(
            [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{json .}}",
                *by_id,
            ]
        )
    )
    memory_by_id: dict[str, int] = {}
    for row in statistics:
        container_id = str(row.get("ID") or row.get("Container") or "")
        usage = str(row.get("MemUsage") or "").split("/", 1)[0].strip()
        if container_id and usage:
            memory_by_id[container_id] = memory_bytes(usage)

    services: dict[str, Any] = {}
    for container_id, row in by_id.items():
        service = str(row.get("Service") or row.get("Name") or container_id)
        inspect = _run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.RestartCount}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}|{{.Image}}",
                container_id,
            ]
        ).split("|", 3)
        services[service] = {
            "container_id": container_id,
            "restart_count": int(inspect[0]),
            "state": inspect[1],
            "health": inspect[2],
            "image_id": inspect[3],
            "memory_bytes": memory_by_id.get(container_id),
        }

    data_bytes = int(
        _run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "analysis-worker",
                "python",
                "-c",
                "import os; print(sum(f.stat().st_size for r,_,n in os.walk('/data') for x in n if (f:=__import__('pathlib').Path(r,x)).is_file()))",
            ]
        ).splitlines()[-1]
    )
    return {
        "recorded_ts": datetime.now(timezone.utc).isoformat(),
        "services": services,
        "operator": _operator_json(),
        "data_bytes": data_bytes,
    }


def _active_queue(status: Mapping[str, Any], queue: str) -> int:
    counts = (((status.get("operator") or {}).get("queues") or {}).get(queue) or {}).get("counts") or {}
    return int(counts.get("queued", 0)) + int(counts.get("running", 0))


def _quarter(values: list[int], *, last: bool) -> list[int]:
    size = max(1, math.ceil(len(values) / 4))
    return values[-size:] if last else values[:size]


def evaluate_samples(
    samples: list[dict[str, Any]],
    *,
    minimum_duration_seconds: int,
    maximum_gap_seconds: int,
    maximum_queue_growth: int,
    maximum_disk_growth_bytes: int,
    maximum_memory_growth_bytes: int,
) -> dict[str, Any]:
    errors: list[str] = []
    if len(samples) < 2:
        return {"status": "failed", "errors": ["at least two samples are required"]}
    timestamps = [datetime.fromisoformat(str(item["recorded_ts"])) for item in samples]
    duration = (timestamps[-1] - timestamps[0]).total_seconds()
    gaps = [(right - left).total_seconds() for left, right in zip(timestamps, timestamps[1:])]
    if duration < minimum_duration_seconds:
        errors.append(f"duration {duration:.0f}s is below required {minimum_duration_seconds}s")
    if max(gaps) > maximum_gap_seconds:
        errors.append(f"sample gap {max(gaps):.0f}s exceeds {maximum_gap_seconds}s")

    seen_services = set.intersection(*(set(sample.get("services") or {}) for sample in samples))
    missing = sorted(EXPECTED_LONG_RUNNING - seen_services)
    if missing:
        errors.append("missing long-running services: " + ", ".join(missing))
    memory_growth: dict[str, int] = {}
    for service in sorted(seen_services):
        rows = [(sample.get("services") or {})[service] for sample in samples]
        if any(row.get("state") != "running" for row in rows):
            errors.append(f"{service} was not running in every sample")
        if any(row.get("health") not in {"healthy", "none"} for row in rows):
            errors.append(f"{service} reported unhealthy status")
        restart_counts = [int(row.get("restart_count", 0)) for row in rows]
        if max(restart_counts) != min(restart_counts):
            errors.append(f"{service} restart count increased")
        image_ids = {str(row.get("image_id") or "") for row in rows}
        if "" in image_ids:
            errors.append(f"{service} image identity is missing")
        elif len(image_ids) != 1:
            errors.append(f"{service} image changed during soak")
        memory = [int(row["memory_bytes"]) for row in rows if row.get("memory_bytes") is not None]
        if len(memory) != len(rows):
            errors.append(f"{service} memory evidence is missing")
        else:
            growth = int(median(_quarter(memory, last=True)) - median(_quarter(memory, last=False)))
            memory_growth[service] = growth
            if growth > maximum_memory_growth_bytes:
                errors.append(f"{service} memory growth {growth} exceeds {maximum_memory_growth_bytes}")

    queue_growth: dict[str, int] = {}
    for queue in ("analysis", "delivery"):
        depths = [_active_queue(sample, queue) for sample in samples]
        growth = int(median(_quarter(depths, last=True)) - median(_quarter(depths, last=False)))
        queue_growth[queue] = growth
        if growth > maximum_queue_growth:
            errors.append(f"{queue} queue growth {growth} exceeds {maximum_queue_growth}")

    disk_growth = int(samples[-1]["data_bytes"]) - int(samples[0]["data_bytes"])
    if disk_growth > maximum_disk_growth_bytes:
        errors.append(f"data growth {disk_growth} exceeds {maximum_disk_growth_bytes}")

    for index, sample in enumerate(samples):
        operator = sample.get("operator") or {}
        if (operator.get("database") or {}).get("status") != "ok":
            errors.append(f"sample {index}: database is not healthy")
        if (operator.get("redis") or {}).get("status") != "ok":
            errors.append(f"sample {index}: Redis is not healthy")
        budget = operator.get("budget") or {}
        if float(budget.get("charged_or_reserved_usd", 0)) > float(budget.get("limit_usd", 20)):
            errors.append(f"sample {index}: LLM budget exceeded")
        analysis = (((operator.get("queues") or {}).get("analysis") or {}).get("counts") or {})
        delivery = (((operator.get("queues") or {}).get("delivery") or {}).get("counts") or {})
        if int(analysis.get("error", 0)) or int(analysis.get("blocked", 0)):
            errors.append(f"sample {index}: analysis terminal work is present")
        if int(delivery.get("dead", 0)) or int(delivery.get("blocked", 0)):
            errors.append(f"sample {index}: delivery terminal work is present")

    return {
        "status": "passed" if not errors else "failed",
        "sample_count": len(samples),
        "duration_seconds": duration,
        "maximum_gap_seconds": max(gaps),
        "queue_growth": queue_growth,
        "memory_growth_bytes": memory_growth,
        "data_growth_bytes": disk_growth,
        "errors": sorted(set(errors)),
    }


def _read_samples(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("collect")
    evaluate = subparsers.add_parser("evaluate")
    evaluate.add_argument("evidence", type=Path)
    evaluate.add_argument("--minimum-duration-seconds", type=int, default=259200)
    evaluate.add_argument("--maximum-gap-seconds", type=int, default=900)
    evaluate.add_argument("--maximum-queue-growth", type=int, default=25)
    evaluate.add_argument("--maximum-disk-growth-bytes", type=int, default=5 * 1024**3)
    evaluate.add_argument("--maximum-memory-growth-bytes", type=int, default=256 * 1024**2)
    args = parser.parse_args()
    if args.command == "collect":
        print(json.dumps(collect_sample(), sort_keys=True))
        return 0
    result = evaluate_samples(
        _read_samples(args.evidence.expanduser().resolve(strict=True)),
        minimum_duration_seconds=args.minimum_duration_seconds,
        maximum_gap_seconds=args.maximum_gap_seconds,
        maximum_queue_growth=args.maximum_queue_growth,
        maximum_disk_growth_bytes=args.maximum_disk_growth_bytes,
        maximum_memory_growth_bytes=args.maximum_memory_growth_bytes,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
