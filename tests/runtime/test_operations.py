from __future__ import annotations

import copy
from pathlib import Path

import pytest


def _production_config(tmp_path):
    from tradingagents.default_config import DEFAULT_CONFIG

    config = copy.deepcopy(DEFAULT_CONFIG)
    config["iic_data_dir"] = str(tmp_path / "data")
    config["iic_db_path"] = str(tmp_path / "data" / "iic.db")
    config["sensing_redis_url"] = "redis://redis:6379/0"
    config["sensing_require_aof_fsync"] = True
    config["sensing_adapters_enabled"] = {
        "polygon_news": True,
        "telegram": True,
        "rss": True,
        "gdelt": False,
        "macro": False,
        "x": False,
    }
    config["orchestrator_enabled"] = True
    config["max_concurrent_jobs"] = 1
    config["delivery"]["enabled_channels"] = ["telegram", "email"]
    config["smtp"]["enabled"] = True
    config["telegram_bot"]["enabled"] = True
    return config


def _production_environment(tmp_path):
    return {
        "POLYGON_API_KEY": "polygon-test",
        "RSS_FEEDS": "https://example.test/feed.xml",
        "TELEGRAM_API_ID": "12345",
        "TELEGRAM_API_HASH": "hash-test",
        "TELEGRAM_SENSING_CHANNELS": "channel_one",
        "TELEGRAM_SENSING_SESSION": str(tmp_path / "data" / "telegram" / "session"),
        "IIC_TELEGRAM_BOT_TOKEN": "bot-test",
        "TELEGRAM_BOT_ALLOWED_CHAT_IDS": "12345",
        "IIC_SMTP_USER": "smtp-user",
        "IIC_SMTP_APP_PASSWORD": "smtp-password",
        "IIC_SMTP_FROM_ADDR": "forge@example.test",
        "IIC_SMTP_TO_ADDRS": "operator@example.test",
        "DEEPSEEK_API_KEY": "llm-test",
    }


@pytest.mark.unit
def test_production_environment_contract_accepts_canonical_values(tmp_path):
    from tradingagents.runtime import validate_production_environment

    assert validate_production_environment(
        _production_config(tmp_path), _production_environment(tmp_path)
    ) == []


@pytest.mark.unit
def test_production_environment_reports_all_missing_credentials(tmp_path):
    from tradingagents.runtime import validate_production_environment

    errors = validate_production_environment(_production_config(tmp_path), {})
    assert any("POLYGON_API_KEY" in error for error in errors)
    assert any("IIC_TELEGRAM_BOT_TOKEN" in error for error in errors)
    assert any("IIC_SMTP_APP_PASSWORD" in error for error in errors)
    assert any("DEEPSEEK_API_KEY" in error for error in errors)


@pytest.mark.unit
def test_production_environment_rejects_invalid_connector_values(tmp_path):
    from tradingagents.runtime import validate_production_environment

    environment = _production_environment(tmp_path)
    environment["RSS_FEEDS"] = "file:///private/feed.xml"
    environment["TELEGRAM_API_ID"] = "not-a-number"
    environment["TELEGRAM_BOT_ALLOWED_CHAT_IDS"] = "not-a-chat"
    environment["IIC_SMTP_TO_ADDRS"] = "one@example.test,two@example.test"
    errors = validate_production_environment(
        _production_config(tmp_path), environment
    )
    assert any("RSS_FEEDS" in error for error in errors)
    assert any("TELEGRAM_API_ID" in error for error in errors)
    assert any("TELEGRAM_BOT_ALLOWED_CHAT_IDS" in error for error in errors)
    assert any("IIC_SMTP_TO_ADDRS" in error for error in errors)


@pytest.mark.unit
def test_production_environment_rejects_wrong_morning_schedule(tmp_path):
    from tradingagents.runtime import validate_production_environment

    config = _production_config(tmp_path)
    config["morning_digest"]["schedule_local_time"] = "07:30"
    errors = validate_production_environment(config, _production_environment(tmp_path))
    assert any("morning digest" in error for error in errors)


@pytest.mark.unit
def test_production_environment_rejects_budget_or_pricing_drift(tmp_path):
    from tradingagents.runtime import validate_production_environment

    config = _production_config(tmp_path)
    config["daily_budget_usd"] = 20.01
    config["daily_budget_timezone"] = "UTC"
    config["quick_think_llm"] = "unpriced-model"
    errors = validate_production_environment(config, _production_environment(tmp_path))
    assert any("combined LLM budget" in error for error in errors)
    assert any("deepseek-v4-flash" in error for error in errors)

    swapped = _production_config(tmp_path)
    swapped["quick_think_llm"] = "deepseek-v4-pro"
    swapped["deep_think_llm"] = "deepseek-v4-flash"
    swapped_errors = validate_production_environment(
        swapped, _production_environment(tmp_path)
    )
    assert any("respectively" in error for error in swapped_errors)


@pytest.mark.unit
def test_production_environment_rejects_unpriced_provider(tmp_path):
    from tradingagents.runtime import validate_production_environment

    config = _production_config(tmp_path)
    config["llm_provider"] = "openai"
    errors = validate_production_environment(config, _production_environment(tmp_path))
    assert any("must be deepseek" in error for error in errors)


@pytest.mark.unit
def test_production_environment_rejects_unpriced_paid_role_override(tmp_path):
    from tradingagents.runtime import validate_production_environment

    config = _production_config(tmp_path)
    config["llm_roles"]["alert_gate"]["provider"] = "openai"
    config["llm_roles"]["alert_gate"]["model"] = "gpt-unpriced"
    errors = validate_production_environment(config, _production_environment(tmp_path))
    assert any("paid LLM role 'alert_gate'" in error for error in errors)


@pytest.mark.unit
def test_production_environment_allows_free_local_role_override(tmp_path):
    from tradingagents.runtime import validate_production_environment

    config = _production_config(tmp_path)
    config["llm_roles"]["triage_salience"]["provider"] = "local"
    config["llm_roles"]["triage_salience"]["model"] = "operator-local-model"
    assert validate_production_environment(
        config, _production_environment(tmp_path)
    ) == []


@pytest.mark.unit
def test_initialize_runtime_bootstraps_private_verified_database(tmp_path):
    from tradingagents.runtime import initialize_runtime

    config = _production_config(tmp_path)
    result = initialize_runtime(
        config,
        require_production_config=True,
        environment=_production_environment(tmp_path),
    )
    database = Path(result["database"])
    assert result["integrity"] == "ok"
    assert result["foreign_key_violations"] == 0
    assert result["migrations"][-1]["version"] == 5
    assert database.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "data" / "events" / "staging").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "data" / "events" / "quarantine").stat().st_mode & 0o777 == 0o700


@pytest.mark.unit
def test_entrypoint_script_is_executable_and_never_contains_secret_values():
    path = Path("docker/entrypoint.sh")
    text = path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o111
    assert "/run/secrets" in text
    assert "exec iic-forge" in text
    assert "set -x" not in text
    assert "DEEPSEEK_API_KEY=" not in text
