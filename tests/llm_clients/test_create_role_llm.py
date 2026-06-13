"""Tests for create_role_llm factory helper.

TDD-order: written before the implementation so the first run should be RED
on the two plan-specified tests plus the extra coverage added here.
"""
import copy
import pytest

from tradingagents.llm_clients.factory import create_role_llm

# ---------------------------------------------------------------------------
# Minimal config fixture shared across tests
# ---------------------------------------------------------------------------

BASE = {
    "llm_provider": "deepseek",
    "quick_think_llm": "deepseek-v4-flash",
    "backend_url": None,
    "llm_roles": {},
}


# ---------------------------------------------------------------------------
# Plan-specified tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_role_falls_back_to_global_when_unset():
    """Role with all-None overrides resolves to global provider + model."""
    cfg = {
        **BASE,
        "llm_roles": {
            "triage_salience": {
                "provider": None,
                "model": None,
                "base_url": None,
                "extra_body": {},
            }
        },
    }
    client = create_role_llm("triage_salience", cfg)
    assert client.provider == "deepseek"
    assert client.model == "deepseek-v4-flash"


@pytest.mark.unit
def test_role_override_wins(monkeypatch):
    """Role with a non-None provider/model uses those values, and extra_body
    reaches the built ChatOpenAI object so thinking can be disabled."""
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    cfg = {
        **BASE,
        "llm_roles": {
            "alert_gate": {
                "provider": "local",
                "model": "qwen3.6-27b-instruct-q4_k_m",
                "base_url": None,
                "extra_body": {
                    "chat_template_kwargs": {"enable_thinking": False}
                },
            }
        },
    }
    client = create_role_llm("alert_gate", cfg)
    assert client.provider == "local"
    assert client.model == "qwen3.6-27b-instruct-q4_k_m"
    llm = client.get_llm()
    # extra_body must reach the langchain model so thinking is disabled.
    assert llm.extra_body["chat_template_kwargs"]["enable_thinking"] is False


# ---------------------------------------------------------------------------
# Extra: missing role raises a descriptive error
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_role_raises_with_name_in_message():
    """Requesting a role not present in llm_roles must raise and name the role."""
    cfg = {
        **BASE,
        "llm_roles": {
            "triage_salience": {"provider": None, "model": None,
                                "base_url": None, "extra_body": {}},
        },
    }
    with pytest.raises(KeyError, match="no_such_role"):
        create_role_llm("no_such_role", cfg)


@pytest.mark.unit
def test_missing_role_includes_available_roles():
    """The KeyError message should name the available roles for easy debugging."""
    cfg = {
        **BASE,
        "llm_roles": {
            "triage_salience": {"provider": None, "model": None,
                                "base_url": None, "extra_body": {}},
            "alert_gate": {"provider": None, "model": None,
                           "base_url": None, "extra_body": {}},
        },
    }
    with pytest.raises(KeyError, match="triage_salience"):
        create_role_llm("bogus_role", cfg)


# ---------------------------------------------------------------------------
# Extra: absent extra_body leaves the ChatOpenAI extra_body unset (or None)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_extra_body_does_not_crash(monkeypatch):
    """A role entry without extra_body (or with extra_body=None) must not
    break the factory for OpenAI-compatible providers."""
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    cfg = {
        **BASE,
        "llm_roles": {
            "triage_salience": {
                "provider": "local",
                "model": "qwen3.6-27b-instruct-q4_k_m",
                "base_url": None,
                # extra_body key entirely absent — client must tolerate this
            }
        },
    }
    client = create_role_llm("triage_salience", cfg)
    llm = client.get_llm()
    # extra_body should not be set (None or absent) when not configured
    assert not llm.extra_body


@pytest.mark.unit
def test_extra_body_none_does_not_set_field(monkeypatch):
    """extra_body=None explicitly must not set extra_body on the ChatOpenAI object."""
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    cfg = {
        **BASE,
        "llm_roles": {
            "triage_salience": {
                "provider": "local",
                "model": "qwen3.6-27b-instruct-q4_k_m",
                "base_url": None,
                "extra_body": None,
            }
        },
    }
    client = create_role_llm("triage_salience", cfg)
    llm = client.get_llm()
    assert not llm.extra_body


# ---------------------------------------------------------------------------
# Extra: config is not mutated after create_role_llm + get_llm
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_config_not_mutated(monkeypatch):
    """create_role_llm + get_llm must not mutate the passed config dict."""
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    cfg = {
        **BASE,
        "llm_roles": {
            "alert_gate": {
                "provider": "local",
                "model": "qwen3.6-27b-instruct-q4_k_m",
                "base_url": None,
                "extra_body": {
                    "chat_template_kwargs": {"enable_thinking": False}
                },
            }
        },
    }
    cfg_snapshot = copy.deepcopy(cfg)
    client = create_role_llm("alert_gate", cfg)
    client.get_llm()
    assert cfg == cfg_snapshot, "create_role_llm mutated the config dict"


# ---------------------------------------------------------------------------
# Extra: global fallback picks up backend_url when base_url not in role
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_role_uses_backend_url_from_global(monkeypatch):
    """When role.base_url is None, backend_url from global config is used."""
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    cfg = {
        **BASE,
        "backend_url": "http://192.168.1.50:8080/v1",
        "llm_roles": {
            "triage_salience": {
                "provider": "local",
                "model": "qwen3.6-27b-instruct-q4_k_m",
                "base_url": None,
                "extra_body": None,
            }
        },
    }
    client = create_role_llm("triage_salience", cfg)
    # base_url from backend_url global must be stored on the client
    assert client.base_url == "http://192.168.1.50:8080/v1"


# ---------------------------------------------------------------------------
# Issue 1: extra_body deep copy — inner dicts must not be shared with config
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extra_body_inner_dict_not_shared_with_config(monkeypatch):
    """After create_role_llm().get_llm(), chat_template_kwargs on the LLM
    object must be a distinct dict from the one in config (deep copy, not
    shallow copy), so that no mutation of the client's extra_body can corrupt
    the caller's config dict.
    """
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    cfg = {
        **BASE,
        "llm_roles": {
            "alert_gate": {
                "provider": "local",
                "model": "qwen3.6-27b-instruct-q4_k_m",
                "base_url": None,
                "extra_body": {
                    "chat_template_kwargs": {"enable_thinking": False}
                },
            }
        },
    }
    llm = create_role_llm("alert_gate", cfg).get_llm()
    # The inner dict must not be the same object as the one in config.
    assert (
        llm.extra_body["chat_template_kwargs"]
        is not cfg["llm_roles"]["alert_gate"]["extra_body"]["chat_template_kwargs"]
    ), (
        "extra_body['chat_template_kwargs'] is shared with config — "
        "factory must use copy.deepcopy, not dict()"
    )


# ---------------------------------------------------------------------------
# Issue 2: mixed-resolution test (provider override + model fallback)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mixed_resolution_provider_override_model_fallback(monkeypatch):
    """Role that overrides provider but leaves model=None falls back to the
    global quick_think_llm for the model field.  This combination also triggers
    the partial-override warning (Issue 3); we capture it here so the test
    does not fail under -W error settings.

    Per-field independence: provider comes from the role entry, model comes
    from the global config.
    """
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    cfg = {
        **BASE,
        "llm_provider": "deepseek",
        "quick_think_llm": "deepseek-v4-flash",
        "llm_roles": {
            "triage_salience": {
                "provider": "local",
                "model": None,
                "base_url": None,
                "extra_body": {},
            }
        },
    }
    # This combination triggers the partial-override RuntimeWarning.
    with pytest.warns(RuntimeWarning, match="triage_salience"):
        client = create_role_llm("triage_salience", cfg)
    assert client.provider == "local"
    assert client.model == "deepseek-v4-flash"


# ---------------------------------------------------------------------------
# Issue 3: partial-override footgun warning
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_partial_override_warns_when_provider_set_but_model_not(monkeypatch):
    """When a role sets provider but not model, a RuntimeWarning must be
    emitted because the global quick_think_llm may not exist on that provider.
    """
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    cfg = {
        **BASE,
        "llm_roles": {
            "alert_gate": {
                "provider": "local",
                "model": None,
                "base_url": None,
                "extra_body": {},
            }
        },
    }
    with pytest.warns(RuntimeWarning, match="alert_gate"):
        create_role_llm("alert_gate", cfg)


@pytest.mark.unit
def test_full_override_does_not_warn(monkeypatch):
    """When both provider AND model are set on the role, no RuntimeWarning
    must be emitted — the configuration is unambiguous.
    """
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    cfg = {
        **BASE,
        "llm_roles": {
            "alert_gate": {
                "provider": "local",
                "model": "qwen3.6-27b-instruct-q4_k_m",
                "base_url": None,
                "extra_body": {},
            }
        },
    }
    import warnings as _warnings
    with _warnings.catch_warnings():
        _warnings.simplefilter("error", RuntimeWarning)
        # Must not raise — no partial override
        create_role_llm("alert_gate", cfg)


@pytest.mark.unit
def test_all_none_fallback_does_not_warn():
    """When all role fields are None (full global fallback), no RuntimeWarning
    must be emitted — there is no ambiguous partial override.
    """
    cfg = {
        **BASE,
        "llm_roles": {
            "triage_salience": {
                "provider": None,
                "model": None,
                "base_url": None,
                "extra_body": {},
            }
        },
    }
    import warnings as _warnings
    with _warnings.catch_warnings():
        _warnings.simplefilter("error", RuntimeWarning)
        # Must not raise — full global fallback, no partial override
        create_role_llm("triage_salience", cfg)
