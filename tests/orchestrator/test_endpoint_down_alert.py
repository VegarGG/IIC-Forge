"""Task 17 — endpoint-down self-alert.

When a daemon's local-LLM failure counter crosses its threshold, a
"local LLM endpoint down" self-alert is emitted through the operator channel
EXACTLY ONCE (debounced) — not once per cycle.  Recovery (a successful call)
re-arms the latch so the next outage alerts again.

Covers:
  - counter-callback mechanics on ``AvailabilityCounter`` (debounce latch,
    re-arm on success, callback fired OUTSIDE the shared lock, callback
    exceptions never propagate into the daemon);
  - the ``SelfAlerter`` seam (loud log fallback when no transport; transport
    failures swallowed + logged; telegram transport built only from EXISTING
    config/env keys);
  - daemon wiring: the promoter loop emits the alert via its counter
    (mirrors test_promoter_local_availability.py's harness) and triage's
    ``_main`` constructs its counter with the alert seam armed (mirrors
    test_triage_local_availability.py's capture harness);
  - non-blocking send: the transport called from a running event-loop thread
    must return promptly (< 1 s) without stalling the loop.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time

import pytest

from tests.llm_clients.conftest import StubOpenAIServer


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
    """Fully stop a stub mid-test (accept loop AND listening socket) so
    subsequent connections are REFUSED — the 'endpoint died' condition."""
    stub.shutdown()
    stub._server.server_close()


class _RecordingTransport:
    """Operator-channel test double: records every notified message."""

    def __init__(self):
        self.messages: list[str] = []

    def __call__(self, message: str) -> None:
        self.messages.append(message)


def _alerting_counter(transport, *, name="promoter_llm_failures",
                      threshold=3, context=""):
    from tradingagents.llm_clients.availability import AvailabilityCounter
    from tradingagents.ops.self_alert import SelfAlerter

    alerter = SelfAlerter(transport=transport, context=context)
    return AvailabilityCounter(
        name=name,
        alert_threshold=threshold,
        on_threshold=alerter.endpoint_down_callback,
    )


@pytest.fixture(autouse=True)
def _hermetic_local_env(monkeypatch):
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# Counter-callback mechanics: threshold crossing alerts EXACTLY ONCE
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_alert_fires_exactly_once_at_threshold_not_per_failure():
    transport = _RecordingTransport()
    counter = _alerting_counter(transport, threshold=3)

    counter.record_failure(reason="ConnectError: refused")
    counter.record_failure(reason="ConnectError: refused")
    assert transport.messages == [], "alerted BEFORE the threshold crossing"

    counter.record_failure(reason="ConnectError: refused")
    assert len(transport.messages) == 1, "threshold crossing must alert once"

    # The outage persists: further failures must NOT re-alert (debounce).
    counter.record_failure(reason="ConnectError: refused")
    counter.record_failure(reason="ConnectError: refused")
    assert len(transport.messages) == 1, "re-alerted during the same outage"


@pytest.mark.unit
def test_alert_message_identifies_the_endpoint_failure():
    """Alert message must include counter identity, failure reason, and the
    endpoint/model context so the operator knows which service is down."""
    transport = _RecordingTransport()
    counter = _alerting_counter(
        transport, name="promoter_llm_failures", threshold=2,
        context="role=alert_gate provider=local model=qwen3:6b endpoint=http://192.168.1.50:8080/v1",
    )

    counter.record_failure(reason="ConnectError: connection refused")
    counter.record_failure(reason="ConnectError: connection refused")

    assert len(transport.messages) == 1
    msg = transport.messages[0]
    assert "local LLM endpoint down" in msg
    assert "promoter_llm_failures" in msg
    assert "ConnectError: connection refused" in msg
    assert "consecutive=2" in msg
    # Context fields must appear so the operator knows which endpoint died.
    assert "role=alert_gate" in msg
    assert "model=qwen3:6b" in msg
    assert "endpoint=http://192.168.1.50:8080/v1" in msg


@pytest.mark.unit
def test_recovery_rearms_so_the_next_outage_alerts_again():
    transport = _RecordingTransport()
    counter = _alerting_counter(transport, threshold=2)

    # Outage 1 → one alert.
    counter.record_failure(reason="down")
    counter.record_failure(reason="down")
    counter.record_failure(reason="down")
    assert len(transport.messages) == 1

    # Recovery resets the consecutive run AND re-arms the latch.
    counter.record_success()

    # Outage 2 → a second alert (and only one).
    counter.record_failure(reason="down again")
    assert len(transport.messages) == 1, "alerted below threshold after re-arm"
    counter.record_failure(reason="down again")
    counter.record_failure(reason="down again")
    assert len(transport.messages) == 2


@pytest.mark.unit
def test_callback_runs_outside_the_counter_lock():
    """Triage shares the counter's lock with its fallback budget across
    threads — a callback invoked UNDER the lock could deadlock or hold it
    through blocking transport I/O.  Prove the lock is released."""
    from tradingagents.llm_clients.availability import AvailabilityCounter

    observed = {}

    def probe_lock(counter: AvailabilityCounter) -> None:
        acquired = counter._lock.acquire(blocking=False)
        observed["lock_free"] = acquired
        if acquired:
            counter._lock.release()

    counter = AvailabilityCounter(
        name="triage_llm_failures", alert_threshold=1,
        on_threshold=probe_lock,
    )
    counter.record_failure(reason="down")
    assert observed.get("lock_free") is True


@pytest.mark.unit
def test_callback_exception_never_propagates_into_the_daemon(caplog):
    from tradingagents.llm_clients.availability import AvailabilityCounter

    caplog.set_level(logging.ERROR)

    def boom(counter):
        raise RuntimeError("transport exploded")

    counter = AvailabilityCounter(
        name="promoter_llm_failures", alert_threshold=1, on_threshold=boom,
    )
    counter.record_failure(reason="down")  # must NOT raise
    assert counter.consecutive == 1
    assert "promoter_llm_failures" in caplog.text


@pytest.mark.unit
def test_counter_without_alert_wiring_never_fires():
    """Default construction (no threshold/callback) keeps Task 15 behavior."""
    from tradingagents.llm_clients.availability import AvailabilityCounter

    counter = AvailabilityCounter(name="promoter_llm_failures")
    for _ in range(10):
        counter.record_failure(reason="down")
    assert counter.consecutive == 10  # nothing raised, nothing fired


# ---------------------------------------------------------------------------
# SelfAlerter: transport fallback + failure handling
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_self_alerter_without_transport_logs_loudly(caplog):
    from tradingagents.ops.self_alert import SelfAlerter

    caplog.set_level(logging.CRITICAL)
    SelfAlerter(transport=None).notify("local LLM endpoint down: test")
    assert "local LLM endpoint down: test" in caplog.text


@pytest.mark.unit
def test_self_alerter_transport_failure_is_swallowed_and_logged(caplog):
    from tradingagents.ops.self_alert import SelfAlerter

    caplog.set_level(logging.CRITICAL)

    def failing_transport(message):
        raise ConnectionError("telegram unreachable")

    SelfAlerter(transport=failing_transport).notify("msg")  # must NOT raise
    # The message is still loud in the logs even when the transport died.
    assert "msg" in caplog.text


@pytest.mark.unit
def test_telegram_transport_unconfigured_returns_none(monkeypatch):
    from tradingagents.ops.self_alert import telegram_transport

    monkeypatch.delenv("IIC_TELEGRAM_BOT_TOKEN", raising=False)
    # No token / no chat ids / disabled — every gap means log-only.
    assert telegram_transport({}) is None
    assert telegram_transport(
        {"telegram_bot": {"enabled": True, "allowed_chat_ids": [123]}}
    ) is None  # token missing
    monkeypatch.setenv("IIC_TELEGRAM_BOT_TOKEN", "tok")
    assert telegram_transport(
        {"telegram_bot": {"enabled": True, "allowed_chat_ids": []}}
    ) is None  # no destination chat
    assert telegram_transport(
        {"telegram_bot": {"enabled": False, "allowed_chat_ids": [123]}}
    ) is None  # disabled


@pytest.mark.unit
def test_telegram_transport_sends_to_first_allowed_chat(monkeypatch):
    """Transport routes the message to the first allowed chat_id via PTB Bot.

    The new implementation builds a fresh Bot per send (no _get_bot cache
    use), so we patch ``telegram.Bot`` directly at the PTB import boundary.
    """
    from tradingagents.ops.self_alert import telegram_transport
    import telegram as tg_pkg

    sent = {}

    class _FakeBot:
        def __init__(self, *, token, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_message(self, *, chat_id, text, **kwargs):
            sent["chat_id"] = chat_id
            sent["text"] = text

    monkeypatch.setenv("IIC_TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(tg_pkg, "Bot", _FakeBot)

    transport = telegram_transport(
        {"telegram_bot": {"enabled": True, "allowed_chat_ids": [42, 99]}}
    )
    assert transport is not None
    transport("local LLM endpoint down: counter=promoter_llm_failures")

    assert sent["chat_id"] == 42
    assert "local LLM endpoint down" in sent["text"]


# ---------------------------------------------------------------------------
# Fix 1: non-blocking send from a running event-loop thread
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_telegram_transport_non_blocking_from_event_loop(monkeypatch):
    """When the transport is called from inside a running coroutine (the
    triage event-loop thread), it must return promptly — NOT block the loop
    for up to 30 s (the old run_coroutine_threadsafe(...).result(timeout=30)
    hazard).

    RED against the old _run_coro blocking path; GREEN after the daemon-thread
    fix.  We assert elapsed < 1 s.  A recording async stub is used for the PTB
    boundary so no real network call is made.
    """
    import telegram as tg_pkg
    from tradingagents.ops.self_alert import telegram_transport

    sent = {}

    class _FakeBot:
        def __init__(self, *, token, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def send_message(self, *, chat_id, text, **kwargs):
            sent["chat_id"] = chat_id
            sent["text"] = text

    monkeypatch.setenv("IIC_TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(tg_pkg, "Bot", _FakeBot)

    transport = telegram_transport(
        {"telegram_bot": {"enabled": True, "allowed_chat_ids": [7]}}
    )
    assert transport is not None

    elapsed_holder: list[float] = []

    async def _run_from_loop():
        t0 = time.monotonic()
        transport("msg from loop")
        elapsed_holder.append(time.monotonic() - t0)

    asyncio.run(_run_from_loop())

    assert elapsed_holder, "elapsed was never recorded"
    assert elapsed_holder[0] < 1.0, (
        f"transport call from event-loop thread took {elapsed_holder[0]:.2f}s "
        f"(expected < 1 s — loop must not be blocked)"
    )


# ---------------------------------------------------------------------------
# Daemon wiring: the promoter loop emits the alert via its counter
# ---------------------------------------------------------------------------

# Distinctive poll interval so the patched time.sleep can tell the promoter's
# loop-tail sleep apart from openai's fractional retry-backoff sleeps.
_POLL_SENTINEL = 77.0


class _SleepCtl:
    """Patched time.sleep: runs side effects per poll cycle, then interrupts
    after exactly ``stop_after`` cycles (KeyboardInterrupt out of main)."""

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


def _seed_candidate(db_path: str, ev_id: str = "ev1") -> None:
    from datetime import datetime, timezone

    from tradingagents.persistence import store
    from tradingagents.persistence.db import connect

    c = connect(db_path)
    store.upsert_watchlist(c, ticker="AAPL", ttl_until=None, tags=["user"])
    store.insert_event(
        c, event_id=ev_id, source="rss",
        ingested_ts=datetime.now(timezone.utc).isoformat(),
        salience=0.9, raw_path=None, status="triaged", deduped_of=None,
    )
    store.insert_event_ticker(c, event_id=ev_id, ticker="AAPL", confidence=0.9)
    c.close()


@pytest.mark.unit
def test_promoter_emits_endpoint_down_alert_once(tmp_path, monkeypatch):
    """Endpoint dies at runtime → cycles 2..4 fail; with fallback_threshold=2
    the operator channel receives EXACTLY ONE alert (not one per cycle)."""
    import tradingagents.ops.self_alert as self_alert_mod
    import tradingagents.orchestrator.promoter as promoter_mod

    stub_local = StubOpenAIServer()
    transport = _RecordingTransport()
    monkeypatch.setattr(
        self_alert_mod, "build_self_alerter",
        lambda config, **kw: self_alert_mod.SelfAlerter(
            transport=transport, context=kw.get("context", "")),
    )

    cfg = {
        "iic_db_path": str(tmp_path / "iic.db"),
        "iic_data_dir": str(tmp_path / "data"),
        "alert_approval_gate_enabled": True,
        "promoter_poll_interval_s": _POLL_SENTINEL,
        "llm_roles": {"alert_gate": {
            "provider": "local", "model": "local-gate-model",
            "base_url": stub_local.url + "/v1", "extra_body": None,
            "fallback": "none", "fallback_threshold": 2,
            "fallback_daily_budget": 500,
        }},
    }
    _seed_candidate(cfg["iic_db_path"])

    # Cycle 1 runs against the live stub; the endpoint dies before cycle 2.
    def on_call(n):
        if n == 1:
            _kill_server(stub_local)

    monkeypatch.setattr(promoter_mod.time, "sleep",
                        _SleepCtl(stop_after=4, on_call=on_call))
    try:
        with pytest.raises(KeyboardInterrupt):
            promoter_mod.main(config=cfg)
    finally:
        try:
            _kill_server(stub_local)
        except OSError:
            pass

    # Cycles 2, 3, 4 each failed (threshold=2 crossed at cycle 3) — but the
    # operator channel got exactly ONE alert, identifying the failure.
    assert len(transport.messages) == 1, transport.messages
    msg = transport.messages[0]
    # provider=local → headline must say "local LLM endpoint down"
    assert "local LLM endpoint down" in msg
    assert "promoter_llm_failures" in msg


# ---------------------------------------------------------------------------
# Daemon wiring: triage's _main arms the seam on its counter
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_triage_main_wires_alert_seam_into_counter(tmp_path, monkeypatch):
    """_main constructs its AvailabilityCounter with the self-alert armed:
    driving the captured counter past fallback_threshold emits exactly one
    alert through the operator channel."""
    from tradingagents.default_config import DEFAULT_CONFIG
    import tradingagents.ops.self_alert as self_alert_mod
    import tradingagents.sensing.embeddings as emb_mod
    import tradingagents.sensing.redis_client as redis_mod
    import tradingagents.sensing.triage as triage_mod

    stub_local = StubOpenAIServer()
    transport = _RecordingTransport()
    monkeypatch.setattr(
        self_alert_mod, "build_self_alerter",
        lambda config, **kw: self_alert_mod.SelfAlerter(
            transport=transport, context=kw.get("context", "")),
    )
    try:
        monkeypatch.setattr(redis_mod, "make_redis", lambda url: object())

        class _FakeEmbedder:
            def load(self):
                pass

        monkeypatch.setattr(emb_mod, "SentenceTransformerEmbedder",
                            lambda model: _FakeEmbedder())

        captured = {}

        class _FakeTriage:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setattr(triage_mod, "Triage", _FakeTriage)
        monkeypatch.setattr(asyncio, "run", lambda coro: coro.close())

        monkeypatch.setitem(DEFAULT_CONFIG, "iic_db_path",
                            str(tmp_path / "iic.db"))
        monkeypatch.setitem(DEFAULT_CONFIG, "iic_data_dir",
                            str(tmp_path / "data"))
        monkeypatch.setitem(DEFAULT_CONFIG, "llm_roles", {
            "triage_salience": {
                "provider": "local", "model": "test-local-model",
                "base_url": stub_local.url + "/v1", "extra_body": None,
                "fallback": "none", "fallback_threshold": 2,
                "fallback_daily_budget": 500,
            },
            "alert_gate": {"provider": None, "model": None, "base_url": None,
                           "extra_body": None, "fallback": "none"},
        })

        triage_mod._main()  # probe OK against the live stub — starts normally

        counter = captured.get("availability_counter")
        assert counter is not None
        # Threshold sourced from the role's fallback_threshold (documented).
        assert counter.alert_threshold == 2

        # Two deferred scores (what process_one records) cross the threshold.
        counter.record_failure(reason="deferred: llm_error: ConnectionError")
        counter.record_failure(reason="deferred: llm_error: ConnectionError")
        counter.record_failure(reason="deferred: llm_error: ConnectionError")

        assert len(transport.messages) == 1, transport.messages
        msg = transport.messages[0]
        assert "local LLM endpoint down" in msg
        assert "triage_llm_failures" in msg
    finally:
        stub_local.shutdown()


# ---------------------------------------------------------------------------
# Fix 1: provider-aware alert identity (truthful headline + provider= field)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_local_provider_alert_says_local_llm_endpoint_down():
    """provider=local context → headline must be 'local LLM endpoint down'
    and the context must include provider=local."""
    from tradingagents.ops.self_alert import SelfAlerter

    transport = _RecordingTransport()
    alerter = SelfAlerter(
        transport=transport,
        context="role=triage_salience provider=local model=qwen3.6:6b endpoint=http://192.168.1.50:8080/v1",
    )
    counter = _alerting_counter(transport, threshold=1)
    # Wire the alerter directly to the counter (re-create with this alerter).
    from tradingagents.llm_clients.availability import AvailabilityCounter
    ctr = AvailabilityCounter(
        name="triage_llm_failures", alert_threshold=1,
        on_threshold=alerter.endpoint_down_callback,
    )
    ctr.record_failure(reason="ConnectError: refused")

    assert len(transport.messages) == 1
    msg = transport.messages[0]
    assert "local LLM endpoint down" in msg, (
        f"Expected 'local LLM endpoint down' in message for provider=local; got: {msg!r}"
    )
    assert "provider=local" in msg


@pytest.mark.unit
def test_api_provider_alert_does_not_claim_local():
    """provider=deepseek context → headline must NOT say 'local LLM endpoint
    down'; it should say 'LLM endpoint down' and include provider=deepseek."""
    from tradingagents.llm_clients.availability import AvailabilityCounter
    from tradingagents.ops.self_alert import SelfAlerter

    transport = _RecordingTransport()
    alerter = SelfAlerter(
        transport=transport,
        context="role=alert_gate provider=deepseek model=deepseek-v4-flash endpoint=",
    )
    ctr = AvailabilityCounter(
        name="promoter_llm_failures", alert_threshold=1,
        on_threshold=alerter.endpoint_down_callback,
    )
    ctr.record_failure(reason="ConnectError: refused")

    assert len(transport.messages) == 1
    msg = transport.messages[0]
    assert "local LLM endpoint down" not in msg, (
        f"Alert for provider=deepseek must NOT claim 'local LLM endpoint down'; got: {msg!r}"
    )
    assert "LLM endpoint down" in msg
    assert "provider=deepseek" in msg
