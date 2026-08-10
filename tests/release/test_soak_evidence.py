from datetime import datetime, timedelta, timezone

from scripts.soak_evidence import EXPECTED_LONG_RUNNING, evaluate_samples, memory_bytes


def _sample(at: datetime, *, restart: int = 0, queue: int = 0, memory: int = 100) -> dict:
    services = {
        name: {
            "restart_count": restart,
            "state": "running",
            "health": "healthy",
            "memory_bytes": memory,
            "image_id": "sha256:candidate",
        }
        for name in EXPECTED_LONG_RUNNING
    }
    return {
        "recorded_ts": at.isoformat(),
        "services": services,
        "data_bytes": 1000,
        "operator": {
            "database": {"status": "ok"},
            "redis": {"status": "ok"},
            "budget": {"charged_or_reserved_usd": 1, "limit_usd": 20},
            "queues": {
                "analysis": {"counts": {"queued": queue}},
                "delivery": {"counts": {"queued": queue}},
            },
        },
    }


def _evaluate(samples: list[dict]) -> dict:
    return evaluate_samples(
        samples,
        minimum_duration_seconds=60,
        maximum_gap_seconds=61,
        maximum_queue_growth=2,
        maximum_disk_growth_bytes=100,
        maximum_memory_growth_bytes=100,
    )


def test_memory_bytes_handles_docker_units() -> None:
    assert memory_bytes("1.5MiB") == 1_572_864
    assert memory_bytes("2 GB") == 2_000_000_000


def test_healthy_soak_passes() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    result = _evaluate([_sample(now), _sample(now + timedelta(seconds=60))])
    assert result["status"] == "passed"
    assert result["errors"] == []


def test_restart_and_queue_growth_fail() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    first = _sample(now)
    second = _sample(now + timedelta(seconds=60), restart=1, queue=10)
    result = _evaluate([first, second])
    assert result["status"] == "failed"
    assert any("restart count increased" in error for error in result["errors"])
    assert any("queue growth" in error for error in result["errors"])


def test_missing_memory_and_image_drift_fail() -> None:
    now = datetime(2026, 8, 9, tzinfo=timezone.utc)
    first = _sample(now)
    second = _sample(now + timedelta(seconds=60))
    first["services"]["scheduler"]["memory_bytes"] = None
    second["services"]["redis"]["image_id"] = "sha256:changed"

    result = _evaluate([first, second])

    assert result["status"] == "failed"
    assert "scheduler memory evidence is missing" in result["errors"]
    assert "redis image changed during soak" in result["errors"]
