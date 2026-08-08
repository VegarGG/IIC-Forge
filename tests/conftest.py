"""Hermetic pytest configuration.

Tests must never inherit operator credentials, load the repository's ``.env``,
write to the operator's real data directories, or contact a non-loopback
network endpoint.  Live tests require both explicit credentials and
``--allow-external-network``.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class ExternalNetworkBlocked(RuntimeError):
    """Raised when a test attempts non-loopback network access."""


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)

_OPERATOR_ENV_VARS = (
    *_API_KEY_ENV_VARS,
    "AZURE_OPENAI_DEPLOYMENT_NAME",
    "AZURE_OPENAI_ENDPOINT",
    "FRED_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "IIC_SMTP_APP_PASSWORD",
    "IIC_SMTP_USER",
    "IIC_TELEGRAM_BOT_TOKEN",
    "LOCAL_LLM_API_KEY",
    "LOCAL_LLM_BASE_URL",
    "OLLAMA_BASE_URL",
    "POLYGON_API_KEY",
    "TELEGRAM_API_HASH",
    "TELEGRAM_API_ID",
    "TELEGRAM_BOT_ALLOWED_CHAT_IDS",
    "TELEGRAM_OSINT_SESSION",
    "TELEGRAM_SENSING_CHANNELS",
    "TELEGRAM_SENSING_SESSION",
    "X_BEARER_TOKEN",
)

# This executes before pytest imports test modules.  It prevents
# tradingagents.__init__ from loading the operator's .env during collection
# and gives any collection-time DEFAULT_CONFIG import a disposable data root.
_LIVE_NETWORK_REQUESTED = "--allow-external-network" in sys.argv
_ORIGINAL_OPERATOR_ENV = {
    name: os.environ[name]
    for name in _OPERATOR_ENV_VARS
    if name in os.environ
}
_SESSION_ROOT = (
    Path(tempfile.gettempdir()).resolve()
    / f"iic-forge-pytest-{os.getpid()}"
)
for _env_var in _OPERATOR_ENV_VARS:
    os.environ.pop(_env_var, None)
# Live test skip conditions are evaluated during collection, before fixtures
# run. Restore only explicit API-key variables when the operator supplied the
# opt-in flag; Telegram/SMTP/session credentials stay absent until an
# integration-marked test fixture starts.
if _LIVE_NETWORK_REQUESTED:
    for _env_var in _API_KEY_ENV_VARS:
        if _env_var in _ORIGINAL_OPERATOR_ENV:
            os.environ[_env_var] = _ORIGINAL_OPERATOR_ENV[_env_var]
os.environ.update({
    "TRADINGAGENTS_DISABLE_DOTENV": "1",
    "TRADINGAGENTS_IIC_DB_PATH": str(_SESSION_ROOT / "iic.db"),
    "TRADINGAGENTS_IIC_DATA_DIR": str(_SESSION_ROOT / "data"),
    "TRADINGAGENTS_RESULTS_DIR": str(_SESSION_ROOT / "results"),
    "TRADINGAGENTS_CACHE_DIR": str(_SESSION_ROOT / "cache"),
    "TRADINGAGENTS_MEMORY_LOG_PATH": str(_SESSION_ROOT / "memory" / "memory.md"),
    # Requests and httpx both honor these spellings.  Local contract-test
    # servers must bypass any proxy inherited from the host or CI runner.
    "NO_PROXY": "127.0.0.1,localhost,::1",
    "no_proxy": "127.0.0.1,localhost,::1",
})


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


def pytest_addoption(parser):
    parser.addoption(
        "--allow-external-network",
        action="store_true",
        default=False,
        help=(
            "allow integration-marked tests to use exported credentials and "
            "contact non-loopback network endpoints"
        ),
    )


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch, tmp_path, request):
    """Give every test fake credentials and disposable state paths."""
    for env_var in _OPERATOR_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    live_integration = (
        request.config.getoption("--allow-external-network")
        and request.node.get_closest_marker("integration") is not None
    )
    if live_integration:
        for env_var, value in _ORIGINAL_OPERATOR_ENV.items():
            monkeypatch.setenv(env_var, value)
    else:
        for env_var in _API_KEY_ENV_VARS:
            monkeypatch.setenv(env_var, "placeholder")
    monkeypatch.setenv("TRADINGAGENTS_DISABLE_DOTENV", "1")
    monkeypatch.setenv("TRADINGAGENTS_IIC_DB_PATH", str(tmp_path / "iic.db"))
    monkeypatch.setenv("TRADINGAGENTS_IIC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRADINGAGENTS_RESULTS_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("TRADINGAGENTS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv(
        "TRADINGAGENTS_MEMORY_LOG_PATH",
        str(tmp_path / "memory" / "memory.md"),
    )
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1")


def _is_loopback_host(host) -> bool:
    if host in (None, ""):
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    normalized = str(host).strip().lower().split("%", 1)[0]
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _require_loopback(address) -> None:
    # A string/bytes address is an AF_UNIX socket path, not a network host.
    if isinstance(address, (str, bytes)):
        return
    if isinstance(address, tuple) and address and _is_loopback_host(address[0]):
        return
    raise ExternalNetworkBlocked(
        f"external network blocked during tests: {address!r}; "
        "use a localhost stub or pass --allow-external-network explicitly"
    )


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch, request):
    """Block DNS and socket traffic except loopback contract-test servers."""
    if (
        request.config.getoption("--allow-external-network")
        and request.node.get_closest_marker("integration") is not None
    ):
        return

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo
    original_sendto = socket.socket.sendto

    def guarded_connect(sock, address):
        _require_loopback(address)
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        _require_loopback(address)
        return original_connect_ex(sock, address)

    def guarded_create_connection(address, *args, **kwargs):
        _require_loopback(address)
        return original_create_connection(address, *args, **kwargs)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not _is_loopback_host(host):
            raise ExternalNetworkBlocked(
                f"external DNS blocked during tests: {host!r}"
            )
        return original_getaddrinfo(host, *args, **kwargs)

    def guarded_sendto(sock, data, *args):
        if not args:
            raise TypeError("sendto expected an address")
        _require_loopback(args[-1])
        return original_sendto(sock, data, *args)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)


def pytest_sessionfinish(session, exitstatus):
    """Remove only the disposable collection-time state created above."""
    if (
        _SESSION_ROOT.parent == Path(tempfile.gettempdir()).resolve()
        and _SESSION_ROOT.name.startswith("iic-forge-pytest-")
    ):
        shutil.rmtree(_SESSION_ROOT, ignore_errors=True)


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "tradingagents.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client
