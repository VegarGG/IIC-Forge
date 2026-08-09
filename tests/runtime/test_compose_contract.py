from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))


@pytest.mark.unit
def test_compose_defines_exact_approved_ingestion_services(compose):
    services = compose["services"]
    sense_services = {name for name in services if name.startswith("sense-")}
    assert sense_services == {"sense-rss", "sense-telegram", "sense-polygon"}


@pytest.mark.unit
def test_compose_contains_every_critical_runtime_service(compose):
    required = {
        "redis",
        "volume-init",
        "database-init",
        "ticker-seed",
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
    }
    assert required <= set(compose["services"])


@pytest.mark.unit
def test_compose_keeps_redis_private_and_persistent(compose):
    redis = compose["services"]["redis"]
    assert "ports" not in redis
    assert "iic-redis:/data" in redis["volumes"]
    assert "./ops/redis/redis.conf:/etc/redis/redis.conf:ro" in redis["volumes"]
    assert redis["healthcheck"]["test"] == ["CMD", "redis-cli", "ping"]


@pytest.mark.unit
def test_redis_allows_compose_peers_without_exposing_a_host_port():
    config = Path("ops/redis/redis.conf").read_text(encoding="utf-8")
    assert "protected-mode no" in config
    assert "appendonly yes" in config
    assert "maxmemory-policy noeviction" in config


@pytest.mark.unit
def test_compose_requires_clean_init_and_seed_before_workers(compose):
    services = compose["services"]
    assert services["database-init"]["command"][-1] == "--require-production-config"
    for name in {
        "sense-rss",
        "sense-telegram",
        "sense-polygon",
        "triage",
        "promoter",
        "analysis-worker",
        "delivery-worker",
        "telegram-bot",
        "action-handler",
        "scheduler",
    }:
        dependencies = services[name]["depends_on"]
        assert dependencies["redis"]["condition"] == "service_healthy"
        assert dependencies["database-init"]["condition"] == "service_completed_successfully"
        assert dependencies["ticker-seed"]["condition"] == "service_completed_successfully"


@pytest.mark.unit
def test_compose_hardens_non_root_application_services(compose):
    for name, service in compose["services"].items():
        if name in {"redis", "volume-init"}:
            continue
        assert service["user"] == "1000:1000"
        assert service["read_only"] is True
        assert "ALL" in service["cap_drop"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert "iic-data:/data" in service["volumes"]
        assert service["restart"] in {"unless-stopped", "no", "on-failure:5"}


@pytest.mark.unit
def test_compose_mounts_secrets_as_files_not_environment_values(compose):
    common = compose["x-app-common"]
    assert set(common["secrets"]) == set(compose["secrets"])
    environment = common["environment"]
    assert "DEEPSEEK_API_KEY" not in environment
    assert "POLYGON_API_KEY" not in environment
    assert "IIC_TELEGRAM_BOT_TOKEN" not in environment
    assert "IIC_SMTP_APP_PASSWORD" not in environment
