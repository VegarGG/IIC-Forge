from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_service_platform_runbook_covers_launch_and_rollback():
    text = (ROOT / "ops" / "runbooks" / "service-platform.md").read_text()
    required = [
        "docker compose --profile runtime --profile sources --profile dashboard up -d",
        "python scripts/focused_soak_gate.py --mode preflight --json",
        "Old Service Shutdown",
        "Redis Ownership",
        "External Local LLM",
        "Deferred Salience Retry",
        "Delivery Fallback",
        "Rollback",
        "TRADINGAGENTS_IIC_DB_PATH=/srv/iic-forge/data/iic.db",
        "Prerequisites",
    ]
    for needle in required:
        assert needle in text, f"Missing required string: {needle!r}"
    assert "/home/ziwei-huang/TradingAgents/TradingAgents" not in text
    assert "iic-redis" not in text
    assert "no_unexpected_api_classification_spend" in text
