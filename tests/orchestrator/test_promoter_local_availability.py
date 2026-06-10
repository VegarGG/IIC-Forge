"""Task 15 (D5) — promoter availability policy tests.

Covers:
  - eager startup probe: ``main`` refuses to start the alert_gate role with
    ``fallback="none"`` and a dead endpoint; with ``fallback="api"`` it starts
    and routes gate calls to the global provider under the daily budget;
  - runtime: on a per-call transport failure the promoter SKIPS the cycle and
    increments the persistent failure counter (no bare-Exception swallowing);
  - ``fallback="api"``: after N consecutive failures the role re-resolves to
    the global API provider; budget exhaustion reverts to skipping.

The local endpoint is the StubOpenAIServer (alive) or a bind-and-close dead
port; "endpoint dies at runtime" is simulated by shutting the stub down from
the patched ``time.sleep`` between poll cycles.
"""

from __future__ import annotations

import logging
import socket
from datetime import datetime, timezone

import pytest

from tests.llm_clients.conftest import StubOpenAIServer
from tradingagents.persistence import store
from tradingagents.persistence.db import connect


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _dead_base_url() -> str:
    """Reserve a port via bind-and-close so connections are refused."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/v1"


def _kill_server(stub: StubOpenAIServer) -> None:
    """Fully stop a stub mid-test: stop the accept loop AND close the
    listening socket.  ``shutdown()`` alone leaves the bound socket accepting
    TCP handshakes whose requests then hang (no accept loop to serve them);
    ``server_close()`` makes subsequent connections be REFUSED immediately,
    which is the 'endpoint died' condition the runtime tests simulate."""
    stub.shutdown()
    stub._server.server_close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_candidate(db_path: str, ev_id: str = "ev1") -> None:
    c = connect(db_path)
    store.upsert_watchlist(c, ticker="AAPL", ttl_until=None, tags=["user"])
    store.insert_event(c, event_id=ev_id, source="rss", ingested_ts=_now(),
                       salience=0.9, raw_path=None, status="triaged",
                       deduped_of=None)
    store.insert_event_ticker(c, event_id=ev_id, ticker="AAPL", confidence=0.9)
    c.close()


def _gate_role(**overrides):
    entry = {
        "provider": None, "model": None, "base_url": None,
        "extra_body": None, "fallback": "none",
        "fallback_threshold": 3, "fallback_daily_budget": 500,
    }
    entry.update(overrides)
    return entry


# Distinctive poll interval so the patched time.sleep can tell the promoter's
# loop-tail sleep apart from openai's fractional retry-backoff sleeps (which
# also go through time.sleep — counting those would end the loop early).
_POLL_SENTINEL = 77.0


def _cfg(tmp_path, *, gate_role, **extra):
    cfg = {
        "iic_db_path": str(tmp_path / "iic.db"),
        "iic_data_dir": str(tmp_path / "data"),
        "alert_approval_gate_enabled": True,
        "promoter_poll_interval_s": _POLL_SENTINEL,
        "llm_roles": {"alert_gate": gate_role},
    }
    cfg.update(extra)
    return cfg


class _SleepCtl:
    """Patched time.sleep: runs side effects per poll cycle, then interrupts.

    ``main`` calls time.sleep(_POLL_SENTINEL) once at the tail of every cycle;
    raising KeyboardInterrupt from poll-sleep #stop_after stops the loop after
    exactly ``stop_after`` cycles (KeyboardInterrupt propagates out of main).
    All other sleeps (openai retry backoff, error backoff) return immediately
    and are not counted.
    """

    def __init__(self, stop_after: int, on_call=None):
        self.calls = 0
        self.stop_after = stop_after
        self.on_call = on_call or (lambda n: None)

    def __call__(self, secs):
        if secs != _POLL_SENTINEL:
            return
        self.calls += 1
        self.on_call(self.calls)
        if self.calls >= self.stop_after:
            raise KeyboardInterrupt


@pytest.fixture
def stub_local():
    s = StubOpenAIServer()
    yield s
    s.shutdown()


@pytest.fixture
def stub_global():
    s = StubOpenAIServer()
    yield s
    s.shutdown()


@pytest.fixture(autouse=True)
def _hermetic_local_env(monkeypatch):
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Startup probe
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_main_refuses_start_dead_endpoint_fallback_none(tmp_path, caplog):
    from tradingagents.llm_clients.availability import LocalEndpointUnavailable
    from tradingagents.orchestrator.promoter import main

    caplog.set_level(logging.INFO)
    dead = _dead_base_url()
    cfg = _cfg(tmp_path, gate_role=_gate_role(
        provider="local", model="local-gate-model",
        base_url=dead, fallback="none"))

    with pytest.raises(LocalEndpointUnavailable) as ei:
        main(config=cfg)

    msg = str(ei.value)
    assert dead in msg and "local-gate-model" in msg
    # Startup log must show the resolved endpoint + model identity.
    assert "local-gate-model" in caplog.text
    assert dead in caplog.text


@pytest.mark.unit
def test_main_starts_and_routes_global_with_fallback_api(
    tmp_path, caplog, monkeypatch, stub_global
):
    """Dead local endpoint + fallback='api' → main starts; the gate call is
    served by the GLOBAL provider and counted against the daily budget."""
    import tradingagents.orchestrator.promoter as promoter_mod

    caplog.set_level(logging.INFO)
    dead = _dead_base_url()
    cfg = _cfg(
        tmp_path,
        gate_role=_gate_role(provider="local", model="local-gate-model",
                             base_url=dead, fallback="api"),
        llm_provider="deepseek",
        quick_think_llm="deepseek-v4-flash",
        backend_url=stub_global.url + "/v1",
    )
    _seed_candidate(cfg["iic_db_path"])
    monkeypatch.setattr(promoter_mod.time, "sleep", _SleepCtl(stop_after=1))

    with pytest.raises(KeyboardInterrupt):
        promoter_mod.main(config=cfg)

    body = stub_global.last_request_json
    assert body is not None, "global-provider stub never received the gate call"
    assert body["model"] == "deepseek-v4-flash"
    assert "fallback" in caplog.text.lower()

    check = connect(cfg["iic_db_path"])
    today = datetime.now(timezone.utc).date().isoformat()
    assert store.get_ops_counter(
        check, name=f"promoter_fallback_calls:{today}") == 1
    # The gate evaluated (stub JSON parses but fails schema → reject row).
    n = check.execute("SELECT COUNT(*) FROM alert_evaluations").fetchone()[0]
    assert n == 1


# ---------------------------------------------------------------------------
# Runtime: per-call failure skips the cycle + increments the counter
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_runtime_failure_skips_cycle_and_counts(
    tmp_path, caplog, monkeypatch, stub_local
):
    import tradingagents.orchestrator.promoter as promoter_mod

    caplog.set_level(logging.INFO)
    cfg = _cfg(tmp_path, gate_role=_gate_role(
        provider="local", model="local-gate-model",
        base_url=stub_local.url + "/v1", fallback="none"))
    _seed_candidate(cfg["iic_db_path"])

    # Cycle 1 runs against the live stub; the endpoint dies before cycle 2.
    def on_call(n):
        if n == 1:
            _kill_server(stub_local)

    monkeypatch.setattr(promoter_mod.time, "sleep",
                        _SleepCtl(stop_after=4, on_call=on_call))

    with pytest.raises(KeyboardInterrupt):
        promoter_mod.main(config=cfg)

    check = connect(cfg["iic_db_path"])
    # Cycles 2-4 each failed: counted loudly, persisted.
    assert store.get_ops_counter(check, name="promoter_llm_failures") == 3
    # Skipped cycles wrote NOTHING: only cycle 1 produced an evaluation row.
    n = check.execute("SELECT COUNT(*) FROM alert_evaluations").fetchone()[0]
    assert n == 1
    assert check.execute("SELECT COUNT(*) FROM queue_jobs").fetchone()[0] == 0
    assert "skip" in caplog.text.lower()
    # With fallback="none" the role must NOT re-resolve to the API provider.
    assert "re-resolved" not in caplog.text.lower()


# ---------------------------------------------------------------------------
# Runtime fallback="api": engage after threshold, bounded by daily budget
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_runtime_fallback_api_engages_after_threshold(
    tmp_path, caplog, monkeypatch, stub_local, stub_global
):
    import tradingagents.orchestrator.promoter as promoter_mod

    caplog.set_level(logging.INFO)
    cfg = _cfg(
        tmp_path,
        gate_role=_gate_role(provider="local", model="local-gate-model",
                             base_url=stub_local.url + "/v1", fallback="api",
                             fallback_threshold=2, fallback_daily_budget=500),
        llm_provider="deepseek",
        quick_think_llm="deepseek-v4-flash",
        backend_url=stub_global.url + "/v1",
    )
    _seed_candidate(cfg["iic_db_path"])

    def on_call(n):
        if n == 1:
            _kill_server(stub_local)

    # c1: local ok; c2: fail #1; c3: fail #2 → engage; c4: global serves.
    monkeypatch.setattr(promoter_mod.time, "sleep",
                        _SleepCtl(stop_after=4, on_call=on_call))

    with pytest.raises(KeyboardInterrupt):
        promoter_mod.main(config=cfg)

    body = stub_global.last_request_json
    assert body is not None, "fallback cycle never reached the global provider"
    assert body["model"] == "deepseek-v4-flash"
    assert "fallback" in caplog.text.lower()

    check = connect(cfg["iic_db_path"])
    assert store.get_ops_counter(check, name="promoter_llm_failures") == 2
    today = datetime.now(timezone.utc).date().isoformat()
    assert store.get_ops_counter(
        check, name=f"promoter_fallback_calls:{today}") == 1


@pytest.mark.unit
def test_runtime_fallback_engagement_swaps_secretary_llm(
    tmp_path, caplog, monkeypatch, stub_local, stub_global
):
    """On runtime fallback engagement the Secretary's composing llm must swap
    to the fallback too — otherwise gate evals burn the API budget while
    compose keeps hitting the DEAD local endpoint, so the event never alerts
    and is refetched every cycle until the daily budget bleeds out."""
    import tradingagents.orchestrator.promoter as promoter_mod
    import tradingagents.secretary.service as secretary_mod

    caplog.set_level(logging.INFO)
    captured: dict = {}
    real_secretary = secretary_mod.Secretary

    class _CapturingSecretary(real_secretary):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured["secretary"] = self
            captured["startup_llm"] = kwargs["llm"]

    monkeypatch.setattr(secretary_mod, "Secretary", _CapturingSecretary)

    cfg = _cfg(
        tmp_path,
        gate_role=_gate_role(provider="local", model="local-gate-model",
                             base_url=stub_local.url + "/v1", fallback="api",
                             fallback_threshold=2, fallback_daily_budget=500),
        llm_provider="deepseek",
        quick_think_llm="deepseek-v4-flash",
        backend_url=stub_global.url + "/v1",
    )
    _seed_candidate(cfg["iic_db_path"])

    def on_call(n):
        if n == 1:
            _kill_server(stub_local)

    # c1: local ok; c2: fail #1; c3: fail #2 → engage; c4: global serves.
    monkeypatch.setattr(promoter_mod.time, "sleep",
                        _SleepCtl(stop_after=4, on_call=on_call))

    with pytest.raises(KeyboardInterrupt):
        promoter_mod.main(config=cfg)

    sec = captured["secretary"]
    # The Secretary no longer holds the startup (dead local) llm...
    assert sec._llm is not captured["startup_llm"]
    # ...and a compose-style call through it reaches the GLOBAL provider.
    # (Were it still the startup llm, this invoke would raise
    # openai.APIConnectionError against the killed local stub.)
    out = sec._llm.invoke("secretary-swap-probe")
    assert getattr(out, "content", str(out)) == '{"ok": true}'
    body = stub_global.last_request_json
    assert body is not None
    assert any("secretary-swap-probe" in str(m.get("content", ""))
               for m in body["messages"])


@pytest.mark.unit
def test_runtime_fallback_budget_exhausted_reverts_to_skipping(
    tmp_path, caplog, monkeypatch, stub_local, stub_global
):
    import tradingagents.orchestrator.promoter as promoter_mod

    caplog.set_level(logging.INFO)
    cfg = _cfg(
        tmp_path,
        gate_role=_gate_role(provider="local", model="local-gate-model",
                             base_url=stub_local.url + "/v1", fallback="api",
                             fallback_threshold=2, fallback_daily_budget=0),
        llm_provider="deepseek",
        quick_think_llm="deepseek-v4-flash",
        backend_url=stub_global.url + "/v1",
    )
    _seed_candidate(cfg["iic_db_path"])

    def on_call(n):
        if n == 1:
            _kill_server(stub_local)

    # c1: local ok; c2-c3: fail (engage at #2); c4: budget=0 → skip, not API.
    monkeypatch.setattr(promoter_mod.time, "sleep",
                        _SleepCtl(stop_after=4, on_call=on_call))

    with pytest.raises(KeyboardInterrupt):
        promoter_mod.main(config=cfg)

    # The hard budget bound held: the global provider was NEVER called.
    assert stub_global.last_request_json is None
    assert "budget" in caplog.text.lower()

    check = connect(cfg["iic_db_path"])
    # c2, c3 = transport failures; c4 = budget-exhausted skip. All counted.
    assert store.get_ops_counter(check, name="promoter_llm_failures") == 3
