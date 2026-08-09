"""Docker Compose runtime contract for the private single-operator service."""

from __future__ import annotations

import json
import os
import sqlite3
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from tradingagents.persistence.db import connect


class RuntimeConfigurationError(RuntimeError):
    """The canonical production environment is incomplete or unsafe."""


_REQUIRED_ENVIRONMENT = (
    "POLYGON_API_KEY",
    "RSS_FEEDS",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_SENSING_CHANNELS",
    "TELEGRAM_SENSING_SESSION",
    "IIC_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_ALLOWED_CHAT_IDS",
    "IIC_SMTP_USER",
    "IIC_SMTP_APP_PASSWORD",
    "IIC_SMTP_FROM_ADDR",
    "IIC_SMTP_TO_ADDRS",
)


def _present(environment: Mapping[str, str], name: str) -> bool:
    value = environment.get(name)
    return value is not None and bool(value.strip())


def _validate_feed_urls(value: str) -> bool:
    feeds = [item.strip() for item in value.split(",") if item.strip()]
    return bool(feeds) and all(
        urlparse(item).scheme in {"http", "https"} and bool(urlparse(item).netloc)
        for item in feeds
    )


def _valid_mailbox(value: str) -> bool:
    candidate = value.strip()
    _display, parsed = parseaddr(candidate)
    return bool(
        candidate
        and parsed == candidate
        and "@" in parsed
        and not any(character in candidate for character in ("\r", "\n", "\x00"))
    )


def validate_production_environment(
    config: Mapping[str, Any],
    environment: Mapping[str, str] | None = None,
) -> list[str]:
    """Return every production configuration violation without secret values."""
    env = environment if environment is not None else os.environ
    errors = [
        f"missing required environment value: {name}"
        for name in _REQUIRED_ENVIRONMENT
        if not _present(env, name)
    ]

    provider = str(config.get("llm_provider") or "").strip().lower()
    provider_secrets = {
        "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY",
        "google": "GOOGLE_API_KEY",
        "gemini": "GOOGLE_API_KEY",
    }
    if provider == "local":
        if not _present(env, "LOCAL_LLM_BASE_URL"):
            errors.append("local LLM provider requires LOCAL_LLM_BASE_URL")
    elif provider in provider_secrets:
        required = provider_secrets[provider]
        if not _present(env, required):
            errors.append(f"LLM provider {provider!r} requires {required}")
    else:
        errors.append(f"unsupported production LLM provider: {provider!r}")

    db_path = Path(str(config.get("iic_db_path") or ""))
    data_dir = Path(str(config.get("iic_data_dir") or ""))
    if not db_path.is_absolute() or not data_dir.is_absolute():
        errors.append("database and data paths must be absolute")
    else:
        try:
            db_path.relative_to(data_dir)
        except ValueError:
            errors.append("database path must live inside the persistent data directory")

    redis_url = str(config.get("sensing_redis_url") or "")
    parsed_redis = urlparse(redis_url)
    if parsed_redis.scheme not in {"redis", "rediss"} or not parsed_redis.hostname:
        errors.append("sensing Redis URL must be a valid redis:// or rediss:// URL")
    if parsed_redis.hostname in {"127.0.0.1", "localhost", "::1"}:
        errors.append("Compose services must not use a loopback Redis hostname")
    if not bool(config.get("sensing_require_aof_fsync")):
        errors.append("production ingestion requires Redis AOF fsync fencing")

    adapter_config = config.get("sensing_adapters_enabled") or {}
    approved = {"polygon_news", "telegram", "rss"}
    enabled = {name for name, value in adapter_config.items() if bool(value)}
    if enabled != approved:
        errors.append(
            "enabled sensing adapters must be exactly polygon_news, telegram, and rss"
        )

    if not bool(config.get("orchestrator_enabled")):
        errors.append("the production orchestrator must be enabled")
    if int(config.get("max_concurrent_jobs", 0)) != 1:
        errors.append("the private deployment requires exactly one analysis worker")

    delivery = config.get("delivery") or {}
    if set(delivery.get("enabled_channels") or []) != {"telegram", "email"}:
        errors.append("production delivery channels must be exactly Telegram and email")
    quiet = delivery.get("quiet_hours") or {}
    if (
        quiet.get("enabled") is not True
        or quiet.get("start") != "22:00"
        or quiet.get("end") != "07:00"
        or quiet.get("timezone") != "Asia/Shanghai"
    ):
        errors.append("quiet hours must be 22:00-07:00 Asia/Shanghai")
    if not bool((config.get("telegram_bot") or {}).get("enabled")):
        errors.append("Telegram delivery/callback service must be enabled")
    if not bool((config.get("smtp") or {}).get("enabled")):
        errors.append("SMTP delivery must be enabled")

    digest = config.get("morning_digest") or {}
    if (
        digest.get("schedule_local_time") != "07:00"
        or digest.get("watchlist_source") != "db"
    ):
        errors.append(
            "morning digest must run at 07:00 Asia/Shanghai from the database watchlist"
        )

    rss_feeds = env.get("RSS_FEEDS", "")
    if rss_feeds and not _validate_feed_urls(rss_feeds):
        errors.append("RSS_FEEDS must contain comma-separated HTTP(S) URLs")

    telegram_api_id = env.get("TELEGRAM_API_ID", "")
    if telegram_api_id:
        try:
            valid_api_id = int(telegram_api_id) > 0
        except ValueError:
            valid_api_id = False
        if not valid_api_id:
            errors.append("TELEGRAM_API_ID must be a positive integer")

    allowed_chat_ids = env.get("TELEGRAM_BOT_ALLOWED_CHAT_IDS", "")
    if allowed_chat_ids:
        try:
            parsed_chat_ids = [
                int(item.strip())
                for item in allowed_chat_ids.split(",")
                if item.strip()
            ]
        except ValueError:
            parsed_chat_ids = []
        if len(parsed_chat_ids) != 1:
            errors.append(
                "TELEGRAM_BOT_ALLOWED_CHAT_IDS must contain exactly one numeric operator chat id"
            )

    smtp_from = env.get("IIC_SMTP_FROM_ADDR", "")
    smtp_to = [
        item.strip()
        for item in env.get("IIC_SMTP_TO_ADDRS", "").split(",")
        if item.strip()
    ]
    if smtp_from and not _valid_mailbox(smtp_from):
        errors.append("IIC_SMTP_FROM_ADDR must be one valid mailbox")
    if smtp_to and (len(smtp_to) != 1 or not _valid_mailbox(smtp_to[0])):
        errors.append("IIC_SMTP_TO_ADDRS must contain exactly one valid operator mailbox")

    telegram_session = env.get("TELEGRAM_SENSING_SESSION", "")
    if telegram_session:
        session_path = Path(telegram_session)
        if not session_path.is_absolute():
            errors.append("TELEGRAM_SENSING_SESSION must be an absolute persistent path")
        elif data_dir.is_absolute():
            try:
                session_path.relative_to(data_dir)
            except ValueError:
                errors.append(
                    "TELEGRAM_SENSING_SESSION must live inside the persistent data directory"
                )

    smtp = config.get("smtp") or {}
    try:
        smtp_port_valid = 1 <= int(smtp.get("port", 0)) <= 65535
    except (TypeError, ValueError):
        smtp_port_valid = False
    if not str(smtp.get("host") or "").strip() or not smtp_port_valid:
        errors.append("SMTP host and port must be valid")
    return errors


def initialize_runtime(
    config: Mapping[str, Any],
    *,
    require_production_config: bool = False,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create private data paths, apply migrations, and verify SQLite."""
    if require_production_config:
        errors = validate_production_environment(config, environment)
        if errors:
            rendered = "\n - ".join(errors)
            raise RuntimeConfigurationError(
                f"production configuration failed:\n - {rendered}"
            )

    data_dir = Path(str(config["iic_data_dir"])).expanduser().resolve()
    db_path = Path(str(config["iic_db_path"])).expanduser().resolve()
    directories = (
        data_dir,
        data_dir / "briefs",
        data_dir / "cache",
        data_dir / "events",
        data_dir / "events" / "staging",
        data_dir / "logs",
        data_dir / "memory",
        data_dir / "reports",
        data_dir / "telegram",
    )
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)

    connection = connect(str(db_path))
    try:
        integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        migrations = [
            {"version": int(row[0]), "name": str(row[1])}
            for row in connection.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            )
        ]
    finally:
        connection.close()
    os.chmod(db_path, 0o600)
    if integrity != ["ok"] or foreign_keys:
        raise RuntimeError(
            f"database verification failed: integrity={integrity!r} "
            f"foreign_keys={len(foreign_keys)}"
        )
    return {
        "database": str(db_path),
        "integrity": "ok",
        "foreign_key_violations": 0,
        "migrations": migrations,
    }


def check_database(config: Mapping[str, Any]) -> dict[str, Any]:
    """Perform a cheap, read-only liveness and migration-version check."""
    from tradingagents.persistence.db import _load_migrations

    path = Path(str(config["iic_db_path"])).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"database does not exist: {path}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=3)
    try:
        latest = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    expected = _load_migrations()[-1]
    if latest is None or (
        int(latest[0]), str(latest[1]), str(latest[2])
    ) != (expected.version, expected.name, expected.checksum):
        raise RuntimeError(
            "database migration does not match the packaged runtime: "
            f"found={latest!r} expected={(expected.version, expected.name)!r}"
        )
    return {"status": "ok", "migration": [expected.version, expected.name]}


def check_redis(config: Mapping[str, Any]) -> dict[str, Any]:
    """Verify Redis reachability and the ingestion durability policy."""
    import redis

    client = redis.Redis.from_url(
        str(config["sensing_redis_url"]),
        socket_connect_timeout=3,
        socket_timeout=3,
        decode_responses=True,
    )
    try:
        if not client.ping():
            raise RuntimeError("Redis PING returned false")
        persistence = client.info("persistence")
        policy = client.config_get("maxmemory-policy").get("maxmemory-policy")
    finally:
        client.close()
    if int(persistence.get("aof_enabled", 0)) != 1:
        raise RuntimeError("Redis AOF is not enabled")
    if persistence.get("aof_last_write_status") not in {None, "ok"}:
        raise RuntimeError("Redis reports a failed AOF write")
    if policy != "noeviction":
        raise RuntimeError(f"Redis maxmemory policy is not noeviction: {policy!r}")
    return {"status": "ok", "aof_enabled": True, "maxmemory_policy": policy}


def run_named_service(name: str) -> None:
    """Run one supported long-lived process using its existing entry point."""
    normalized = name.strip().lower()
    if normalized == "rss":
        from tradingagents.sensing.adapters.rss import _main

        _main()
    elif normalized == "telegram-ingest":
        from tradingagents.sensing.adapters.telegram import _main

        _main()
    elif normalized == "polygon":
        from tradingagents.sensing.adapters.polygon_news import _main

        _main()
    elif normalized == "triage":
        from tradingagents.sensing.triage import _main

        _main()
    elif normalized == "telegram-bot":
        from tradingagents.delivery.telegram_bot import main

        main()
    elif normalized == "scheduler":
        from tradingagents.runtime.scheduler import main

        main()
    else:
        raise RuntimeConfigurationError(f"unknown production service: {name!r}")


def render_health_payload(config: Mapping[str, Any]) -> str:
    """Return a stable JSON health payload for CLI and container probes."""
    return json.dumps(
        {"database": check_database(config), "redis": check_redis(config)},
        sort_keys=True,
    )
