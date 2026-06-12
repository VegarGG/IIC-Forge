from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_compose_defines_iic_owned_runtime_services():
    data = yaml.safe_load((ROOT / "compose.yml").read_text())
    assert data["name"] == "iic-forge"
    services = data["services"]
    expected = {
        "redis",
        "adapter-polygon",
        "adapter-telegram",
        "adapter-x",
        "adapter-rss",
        "adapter-gdelt",
        "adapter-macro",
        "triage",
        "promoter",
        "worker-action",
        "worker-deep",
        "action-handler",
        "delivery",
        "dashboard",
        "gate-runner",
    }
    assert expected.issubset(services.keys())
    assert "iic-redis" not in "\n".join(services.keys())
    assert services["redis"]["image"].startswith("redis:7")
    assert "iic_redis_data:/data" in services["redis"]["volumes"]
    assert "./ops/redis/redis.conf:/usr/local/etc/redis/redis.conf:ro" in services["redis"]["volumes"]
    assert services["redis"]["command"] == ["redis-server", "/usr/local/etc/redis/redis.conf"]
    assert services["triage"]["depends_on"]["redis"]["condition"] == "service_healthy"
    assert services["promoter"]["depends_on"]["redis"]["condition"] == "service_healthy"
    assert services["dashboard"]["ports"] == ["${DASHBOARD_PORT:-8501}:8501"]
    assert "iic_redis_data" in data["volumes"]
    assert "iic_data" in data["volumes"]


@pytest.mark.unit
def test_compose_keeps_local_llm_external_and_configured_by_env():
    text = (ROOT / "compose.yml").read_text()
    assert "llama" not in text.lower()
    assert "LOCAL_LLM_BASE_URL" in text
    data = yaml.safe_load(text)
    for name in ("triage", "promoter"):
        service = data["services"][name]
        assert "ops/env.iic-forge.example" in service["env_file"]
        assert any("LOCAL_LLM_BASE_URL" in str(item) for item in service.get("environment", []))


@pytest.mark.unit
def test_env_template_covers_launch_configuration():
    text = (ROOT / "ops" / "env.iic-forge.example").read_text()
    required = [
        "TRADINGAGENTS_IIC_DB_PATH=/data/iic.db",
        "TRADINGAGENTS_IIC_DATA_DIR=/data",
        "TRADINGAGENTS_SENSING_REDIS_URL=redis://redis:6379/0",
        "LOCAL_LLM_BASE_URL=http://host.docker.internal:8080/v1",
        "IIC_TRIAGE_LLM_PROVIDER=local",
        "IIC_ALERT_GATE_LLM_PROVIDER=local",
        "IIC_DELIVERY_POLICY=ordered_telegram_email",
        "IIC_WORKER_DEEP_CONCURRENCY=1",
        "IIC_SOURCE_STALE_AFTER_SECONDS=1800",
    ]
    for needle in required:
        assert needle in text
    assert "TradingAgents/TradingAgents" not in text
    assert "iic-redis" not in text
