import pytest


@pytest.mark.unit
def test_default_config_has_f5_keys(monkeypatch):
    import importlib
    import tradingagents.default_config as _dc
    # Assert the COMMITTED defaults, independent of the developer's local .env
    # (which may set TELEGRAM_BOT_ALLOWED_CHAT_IDS / TELEGRAM_SENSING_CHANNELS
    # via the nested-env overrides). Clear them, then reload.
    monkeypatch.delenv("TELEGRAM_BOT_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("TELEGRAM_SENSING_CHANNELS", raising=False)
    C = importlib.reload(_dc).DEFAULT_CONFIG

    # Delivery channels + quiet hours
    assert C["delivery"]["enabled_channels"] == ["telegram", "email"]
    assert C["delivery"]["quiet_hours"]["enabled"] is True
    assert C["delivery"]["quiet_hours"]["start"] == "22:00"
    assert C["delivery"]["quiet_hours"]["end"] == "07:00"
    assert C["delivery"]["quiet_hours"]["timezone"] == "Asia/Shanghai"
    assert C["delivery"]["queue_max_attempts"] == 5
    assert C["delivery"]["digest_modes"]["telegram"] == "terse"
    assert C["delivery"]["digest_modes"]["email"] == "full"
    assert C["delivery"]["digest_modes"]["cli"] == "full"

    # Telegram bot — enabled, but restricted (deny-all until chat ids are
    # supplied via the TELEGRAM_BOT_ALLOWED_CHAT_IDS env var in .env)
    assert C["telegram_bot"]["enabled"] is True
    assert C["telegram_bot"]["allowed_chat_ids"] == []
    assert C["telegram_bot"]["poll_interval_seconds"] == 1

    # SMTP — opt-in
    assert C["smtp"]["enabled"] is False
    assert C["smtp"]["host"] == "smtp.gmail.com"
    assert C["smtp"]["port"] == 587

    # Morning digest
    assert C["morning_digest"]["schedule_local_time"] == "07:00"
    assert C["morning_digest"]["watchlist_source"] == "db"

    # Refinement (classifier_llm removed — IIC-FORGE-05 Task 4, subsumed by llm_roles)
    assert C["refinement"]["max_depth"] == 3
    assert "classifier_llm" not in C["refinement"]
    assert C["refinement"]["action_expires_hours"] == 24

    # Action handler
    assert C["action_handler"]["tick_interval_seconds"] == 5

    # Dashboard — opt-in
    assert C["dashboard"]["enabled"] is False
    assert C["dashboard"]["port"] == 8501
    assert C["dashboard"]["bind_address"] == "127.0.0.1"

    # F5 cost guards (still off per Appendix A)
    assert C["refinement_chain_budget"]["enabled"] is False
    assert C["refinement_chain_budget"]["max_usd_per_chain"] == 10.0
    assert C["morning_digest_token_ceiling"]["enabled"] is False
    assert C["morning_digest_token_ceiling"]["max_in_tokens"] == 500_000


@pytest.mark.unit
def test_smtp_delivery_config_is_env_overridable(monkeypatch):
    import tradingagents.default_config as dc

    monkeypatch.setenv("IIC_SMTP_ENABLED", "true")
    monkeypatch.setenv("IIC_SMTP_HOST", "smtp.test.invalid")
    monkeypatch.setenv("IIC_SMTP_PORT", "2525")
    monkeypatch.setenv("IIC_SMTP_FROM_ADDR", "forge@test.invalid")
    monkeypatch.setenv(
        "IIC_SMTP_TO_ADDRS", "operator@test.invalid, audit@test.invalid"
    )
    config = {
        "smtp": {
            "enabled": False,
            "host": "smtp.gmail.com",
            "port": 587,
            "from_addr": "",
            "to_addrs": [],
        }
    }

    result = dc._apply_nested_env_overrides(config)

    assert result["smtp"] == {
        "enabled": True,
        "host": "smtp.test.invalid",
        "port": 2525,
        "from_addr": "forge@test.invalid",
        "to_addrs": ["operator@test.invalid", "audit@test.invalid"],
    }
