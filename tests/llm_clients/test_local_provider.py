import pytest
from tradingagents.llm_clients.capabilities import get_capabilities
from tradingagents.llm_clients.factory import create_llm_client, _OPENAI_COMPATIBLE
from tradingagents.llm_clients.openai_client import _resolve_provider_base_url
from tradingagents.llm_clients.api_key_env import is_optional_key


@pytest.mark.unit
def test_local_is_openai_compatible():
    assert "local" in _OPENAI_COMPATIBLE


@pytest.mark.unit
def test_local_default_base_url(monkeypatch):
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
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
# Optional-key provider semantics
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_optional_key_providers():
    assert is_optional_key("local") and is_optional_key("ollama")
    assert not is_optional_key("deepseek")


@pytest.mark.unit
def test_local_uses_api_key_when_present(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "sk-lan-secret")
    llm = create_llm_client(provider="local", model="qwen3.6-27b-instruct-q4_k_m").get_llm()
    assert llm.openai_api_key.get_secret_value() == "sk-lan-secret"


# ---------------------------------------------------------------------------
# Regression: required providers must still raise when key is absent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_required_provider_still_raises_without_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        create_llm_client(provider="deepseek", model="deepseek-chat").get_llm()


# ---------------------------------------------------------------------------
# Task 3: capability rows for candidate local classifier models
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("model", [
    "qwen3.6-27b-instruct-q4_k_m",
    "deepseek-v4-flash-gguf-q4_k_m",
])
def test_local_model_caps(model):
    caps = get_capabilities(model)
    assert caps.supports_json_schema is True
    assert caps.preferred_structured_method == "json_schema"
    assert caps.requires_reasoning_content_roundtrip is False


# ---------------------------------------------------------------------------
# Regression: forward-compat GGUF quant variants must match _LOCAL_CLASSIFIER,
# not the broader ^deepseek-v\d thinking-model pattern.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("model", [
    "deepseek-v4-flash-gguf-q5_k_m",
    "qwen3.6-27b-instruct-q8_0",
])
def test_unregistered_gguf_quant_variants_get_local_classifier_caps(model):
    """Unregistered quant suffixes must resolve to _LOCAL_CLASSIFIER, not _DEEPSEEK_THINKING."""
    caps = get_capabilities(model)
    assert caps.supports_json_schema is True, (
        f"{model}: expected _LOCAL_CLASSIFIER caps (supports_json_schema=True), "
        "got False (silently matched _DEEPSEEK_THINKING)"
    )
    assert caps.preferred_structured_method == "json_schema", (
        f"{model}: expected preferred_structured_method='json_schema', got {caps.preferred_structured_method!r}"
    )
    assert caps.requires_reasoning_content_roundtrip is False, (
        f"{model}: expected requires_reasoning_content_roundtrip=False, got True"
    )
