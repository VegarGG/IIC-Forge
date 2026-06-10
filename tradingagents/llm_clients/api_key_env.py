"""Canonical provider -> API-key env-var mapping.

A single source of truth for which environment variable holds the API
key for each supported LLM provider. Used by the CLI's interactive key
prompt (cli/utils.ensure_api_key) and by anything else that needs to
ask "does this provider require a key, and which env var is it?".

When adding a new provider, register its env var here so the CLI flow
prompts for it automatically instead of failing on first API call.
"""

from __future__ import annotations

from typing import Optional


PROVIDER_API_KEY_ENV: dict[str, Optional[str]] = {
    "openai":     "OPENAI_API_KEY",
    "anthropic":  "ANTHROPIC_API_KEY",
    "google":     "GOOGLE_API_KEY",
    "azure":      "AZURE_OPENAI_API_KEY",
    "xai":        "XAI_API_KEY",
    "deepseek":   "DEEPSEEK_API_KEY",
    # Dual-region providers each carry their own account; keys are not
    # interchangeable between the international and China endpoints.
    "qwen":       "DASHSCOPE_API_KEY",
    "qwen-cn":    "DASHSCOPE_CN_API_KEY",
    "glm":        "ZHIPU_API_KEY",
    "glm-cn":     "ZHIPU_CN_API_KEY",
    "minimax":    "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    # Local runtimes: ollama never authenticates; local (llama-server) can
    # optionally authenticate via LOCAL_LLM_API_KEY but does not require it.
    "ollama":     None,
    "local":      "LOCAL_LLM_API_KEY",
}

# Providers where the API key is optional — the server works without one
# (e.g. llama-server on a trusted LAN, or Ollama which never authenticates).
# get_llm must NOT raise when the env var is absent for these providers.
OPTIONAL_KEY_PROVIDERS: frozenset[str] = frozenset({"local", "ollama"})


def is_optional_key(provider: str) -> bool:
    """Return True if the API key is optional for ``provider``.

    Optional-key providers build a working LLM client even when their
    mapped env var is unset. The server *may* require a key (e.g.
    ``llama-server --api-key``) but the client must not fail at
    construction time if it is absent.
    """
    return provider.lower() in OPTIONAL_KEY_PROVIDERS


def get_api_key_env(provider: str) -> Optional[str]:
    """Return the env var name for `provider`'s API key, or None if not applicable.

    Unknown providers also return None — callers should treat that as
    "no key check possible" rather than as "no key required".
    """
    return PROVIDER_API_KEY_ENV.get(provider.lower())
