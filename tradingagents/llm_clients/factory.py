import copy
import warnings
from typing import Any, Dict, Optional

from .base_client import BaseLLMClient

# Providers that use the OpenAI-compatible chat completions API
_OPENAI_COMPATIBLE = (
    "openai", "xai", "deepseek",
    "qwen", "qwen-cn",
    "glm", "glm-cn",
    "minimax", "minimax-cn",
    "ollama", "openrouter",
    "local",
)


def create_llm_client(
    provider: str,
    model: str,
    base_url: Optional[str] = None,
    **kwargs,
) -> BaseLLMClient:
    """Create an LLM client for the specified provider.

    Provider modules are imported lazily so that simply importing this
    factory (e.g. during test collection) does not pull in heavy LLM SDKs
    or fail when their API keys are absent.

    Args:
        provider: LLM provider name
        model: Model name/identifier
        base_url: Optional base URL for API endpoint
        **kwargs: Additional provider-specific arguments

    Returns:
        Configured BaseLLMClient instance

    Raises:
        ValueError: If provider is not supported
    """
    provider_lower = provider.lower()

    if provider_lower in _OPENAI_COMPATIBLE:
        from .openai_client import OpenAIClient
        return OpenAIClient(model, base_url, provider=provider_lower, **kwargs)

    if provider_lower == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(model, base_url, **kwargs)

    if provider_lower == "google":
        from .google_client import GoogleClient
        return GoogleClient(model, base_url, **kwargs)

    if provider_lower == "azure":
        from .azure_client import AzureOpenAIClient
        return AzureOpenAIClient(model, base_url, **kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider}")


def create_role_llm(role: str, config: Dict[str, Any]) -> BaseLLMClient:
    """Resolve a role name to a fully configured LLM client.

    Resolution order (or-fallback, so empty string is treated as unset):
      1. ``config["llm_roles"][role]["provider"]`` / ``["model"]`` / ``["base_url"]``
      2. ``config["llm_provider"]`` / ``config["quick_think_llm"]`` /
         ``config.get("backend_url")``

    ``extra_body`` from the role entry is forwarded to the underlying
    OpenAI-compatible client so providers like llama-server can receive
    ``chat_template_kwargs`` (e.g. ``enable_thinking=False``).

    The ``fallback`` key present in role entries is intentionally ignored here
    — it is consumed by the availability layer
    (``tradingagents.llm_clients.availability.resolve_role_llm_with_fallback``).
    ``extra_body`` is treated as READ-ONLY; the config dict is never mutated.

    Args:
        role:   Key into ``config["llm_roles"]``.  A missing key is a config
                bug — raises ``KeyError`` naming the role and available roles.
        config: Mapping containing at minimum ``llm_provider``,
                ``quick_think_llm``, and ``llm_roles``.

    Returns:
        Configured :class:`BaseLLMClient` instance.

    Raises:
        KeyError:   ``role`` is absent from ``config["llm_roles"]``.
        ValueError: Resolved provider is not supported.
    """
    llm_roles: Dict[str, Any] = config.get("llm_roles", {})
    if role not in llm_roles:
        available = ", ".join(sorted(llm_roles)) or "<none>"
        raise KeyError(
            f"Role '{role}' is not defined in config['llm_roles']. "
            f"Available roles: {available}"
        )

    override: Dict[str, Any] = llm_roles[role]

    provider = override.get("provider") or config["llm_provider"]
    model = override.get("model") or config["quick_think_llm"]
    base_url = override.get("base_url") or config.get("backend_url")

    # Warn when a role pins the provider but leaves model unset: the global
    # quick_think_llm is used as a fallback, which may not exist on the
    # overridden provider — a common misconfiguration footgun.
    if override.get("provider") and not override.get("model"):
        warnings.warn(
            f"Role '{role}' overrides provider to '{provider}' but model falls "
            f"back to global quick_think_llm '{model}', which may not exist on "
            f"that provider.",
            RuntimeWarning,
            stacklevel=2,
        )

    # Only forward extra_body when truthy so that non-local providers that do
    # not understand the key are not affected.  OpenAI-compatible clients
    # forward it via _PASSTHROUGH_KWARGS; non-compatible clients (Anthropic,
    # Google, Azure) store it in self.kwargs but never iterate it, so they
    # tolerate the kwarg silently.  Deep-copied so that no level of the role's
    # extra_body (which may alias DEFAULT_CONFIG) is shared with the client;
    # the config must remain immutable for the daemon's lifetime.
    extra_body = override.get("extra_body")
    kwargs = {}
    if extra_body:
        kwargs["extra_body"] = copy.deepcopy(extra_body)

    return create_llm_client(
        provider=provider, model=model, base_url=base_url, **kwargs
    )
