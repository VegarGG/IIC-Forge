import pytest
from tradingagents.llm_clients.factory import create_llm_client, _OPENAI_COMPATIBLE
from tradingagents.llm_clients.openai_client import _resolve_provider_base_url


@pytest.mark.unit
def test_local_is_openai_compatible():
    assert "local" in _OPENAI_COMPATIBLE


@pytest.mark.unit
def test_local_default_base_url():
    assert _resolve_provider_base_url("local") == "http://127.0.0.1:8080/v1"


@pytest.mark.unit
def test_local_base_url_env_override(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://192.168.1.50:8080/v1")
    assert _resolve_provider_base_url("local") == "http://192.168.1.50:8080/v1"


@pytest.mark.unit
def test_local_client_builds_without_api_key(monkeypatch):
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    client = create_llm_client(provider="local", model="qwen3.6-27b-instruct-q4_k_m")
    # Building the langchain object must NOT raise on a missing key.
    client.get_llm()


# ---------------------------------------------------------------------------
# Task 2: Optional-key provider semantics
# ---------------------------------------------------------------------------

from tradingagents.llm_clients.api_key_env import OPTIONAL_KEY_PROVIDERS, is_optional_key


@pytest.mark.unit
def test_optional_key_providers():
    assert is_optional_key("local") and is_optional_key("ollama")
    assert not is_optional_key("deepseek")


@pytest.mark.unit
def test_local_uses_api_key_when_present(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "sk-lan-secret")
    llm = create_llm_client(provider="local", model="qwen3.6-27b-instruct-q4_k_m").get_llm()
    assert llm.openai_api_key.get_secret_value() == "sk-lan-secret"
