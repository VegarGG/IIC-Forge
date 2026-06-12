from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_FILES = [
    ROOT / "compose.yml",
    ROOT / "ops" / "env.iic-forge.example",
    ROOT / "ops" / "backup.sh",
    ROOT / "ops" / "presoak.sh",
    ROOT / "ops" / "systemd" / "iic-forge-compose.service",
    ROOT / "ops" / "systemd" / "redis-server.service",
    ROOT / "ops" / "runbooks" / "f3-exit-gate.md",
    ROOT / "ops" / "runbooks" / "f4-exit-gate.md",
    ROOT / "ops" / "runbooks" / "local-llm.md",
    ROOT / "ops" / "redis" / "redis.conf",
]


@pytest.mark.unit
def test_production_ops_files_do_not_reference_old_tree_or_redis_container():
    bad = {}
    for path in PRODUCTION_FILES:
        text = path.read_text()
        hits = [
            needle
            for needle in (
                "/home/ziwei-huang/TradingAgents/TradingAgents",
                "docker start iic-redis",
                "docker exec iic-redis",
                "docker stop iic-redis",
                "REDIS_CONTAINER=${REDIS_CONTAINER:-iic-redis}",
            )
            if needle in text
        ]
        if hits:
            bad[str(path.relative_to(ROOT))] = hits
    assert bad == {}


@pytest.mark.unit
def test_systemd_compose_supervisor_is_single_runtime_entrypoint():
    unit = (ROOT / "ops" / "systemd" / "iic-forge-compose.service").read_text()
    assert "docker compose" in unit
    assert "WorkingDirectory=/opt/iic-forge" in unit
    assert "ExecStart=/usr/bin/docker compose --profile runtime --profile sources --profile dashboard up" in unit
    assert "ExecStop=/usr/bin/docker compose down" in unit
