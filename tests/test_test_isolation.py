"""Contract tests for the hermetic pytest boundary."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.unit
def test_operator_credentials_are_not_inherited():
    assert os.environ["DEEPSEEK_API_KEY"] == "placeholder"
    assert "POLYGON_API_KEY" not in os.environ
    assert "IIC_TELEGRAM_BOT_TOKEN" not in os.environ
    assert "IIC_SMTP_APP_PASSWORD" not in os.environ
    assert "TELEGRAM_BOT_ALLOWED_CHAT_IDS" not in os.environ


@pytest.mark.unit
def test_dotenv_loading_is_disabled():
    assert os.environ["TRADINGAGENTS_DISABLE_DOTENV"] == "1"

    from tradingagents.default_config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["telegram_bot"]["allowed_chat_ids"] == []
    assert DEFAULT_CONFIG["telegram_channels"] == []


@pytest.mark.unit
def test_dotenv_opt_out_blocks_loading_without_changing_default(tmp_path):
    (tmp_path / ".env").write_text(
        "IIC_TELEGRAM_BOT_TOKEN=must-not-load\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["TRADINGAGENTS_DISABLE_DOTENV"] = "1"
    env.pop("IIC_TELEGRAM_BOT_TOKEN", None)
    project_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (project_root, env.get("PYTHONPATH", "")) if part
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, tradingagents; "
            "print(os.environ.get('IIC_TELEGRAM_BOT_TOKEN', ''))",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""

    default_env = env.copy()
    default_env.pop("TRADINGAGENTS_DISABLE_DOTENV", None)
    default_result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, tradingagents; "
            "print(os.environ.get('IIC_TELEGRAM_BOT_TOKEN', ''))",
        ],
        cwd=tmp_path,
        env=default_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert default_result.stdout.strip() == "must-not-load"


@pytest.mark.unit
def test_external_dns_is_blocked():
    with pytest.raises(RuntimeError, match="external DNS blocked"):
        socket.getaddrinfo("example.com", 443)


@pytest.mark.unit
def test_external_ip_connection_is_blocked_before_connect():
    sock = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="external network blocked"):
            sock.connect(("192.0.2.1", 443))
    finally:
        sock.close()


@pytest.mark.unit
def test_loopback_dns_is_allowed():
    records = socket.getaddrinfo("localhost", 0)
    assert records
