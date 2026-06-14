"""D5 availability policy — degrade loudly, fall back deliberately (Task 15).

This module is the single home for everything the triage and promoter daemons
need to survive a local LLM endpoint outage WITHOUT silent degradation:

  - ``probe_local_endpoint`` — eager startup probe (GET /health + 1-token
    completion) run only when a role resolves to ``provider='local'``;
  - ``LocalEndpointUnavailable`` — typed failure carrying endpoint + model;
  - ``TRANSPORT_EXCEPTIONS`` — the NARROW set of exception types that mean
    "the endpoint is unavailable" (never a bare ``Exception``);
  - ``AvailabilityCounter`` / ``DailyFallbackBudget`` — observable failure /
    budget counters, persisted to the ``ops_counters`` table so the L3 soak
    gate and the Task 17 self-alert can read them across restarts;
  - ``resolve_role_llm_with_fallback`` / ``resolve_role_llm_global`` — shared
    role resolution honoring the per-role ``fallback`` config ("none"/"api").

Counter names (see also schema.sql's ops_counters comment):
  TRIAGE_FAILURE_COUNTER / PROMOTER_FAILURE_COUNTER   — monotonic failures
  TRIAGE_FALLBACK_BUDGET / PROMOTER_FALLBACK_BUDGET   — '<name>:<YYYY-MM-DD>'

Counter UNITS differ by daemon — deliberate asymmetry, compare with care:
  - ``triage_llm_failures`` counts per-EVENT deferred scores: every envelope
    whose salience the scorer could not produce, INCLUDING parse_error defers
    (a local model emitting garbage is a triage-health problem even when the
    transport is fine; see triage.process_one).
  - ``promoter_llm_failures`` counts per-CYCLE transport failures only: one
    bump per poll cycle skipped because TRANSPORT_EXCEPTIONS escaped the gate
    call.  A gate evaluation that transports fine but fails to PARSE counts
    NOTHING — it neither increments the counter nor resets the consecutive
    run (not transport-failure evidence, not health evidence either; see the
    ``parse_ok`` gate in promoter.main's alert_evaluator).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

import httpx
import openai
import requests

from tradingagents.persistence import store

log = logging.getLogger(__name__)


# Persistent counter names (ops_counters.name). One failure counter and one
# per-day fallback-budget prefix per daemon, so each daemon's health is
# independently queryable (L3 gate: "failure counter = 0").
TRIAGE_FAILURE_COUNTER = "triage_llm_failures"
PROMOTER_FAILURE_COUNTER = "promoter_llm_failures"
TRIAGE_FALLBACK_BUDGET = "triage_fallback_calls"
PROMOTER_FALLBACK_BUDGET = "promoter_fallback_calls"


class LocalEndpointUnavailable(Exception):
    """The local LLM endpoint failed a probe / budget check.

    The message always carries the resolved endpoint + model identity so the
    operator can act from the log line alone.
    """


# Exception types that mean "the endpoint is unavailable" at runtime.
#
# DELIBERATELY NARROW — never catch bare ``Exception`` here:
#   - tests patch ``factory.create_role_llm`` with a sentinel exception that
#     MUST propagate (a broad except would eat it and mask wiring bugs);
#   - 4xx-style ``openai.APIStatusError`` subclasses (auth, bad request,
#     model-not-found) are CONFIG bugs that must crash loudly, not be skipped
#     as transient unavailability — hence ``InternalServerError`` (HTTP 5xx)
#     rather than the whole ``APIStatusError`` family.
#
# What the runtime stack actually raises: langchain-openai delegates to the
# openai SDK, which wraps all httpx transport failures into
# ``openai.APIConnectionError`` (timeouts into its ``APITimeoutError``
# subclass).  The bare httpx types are included belt-and-braces for call
# sites that hit the endpoint without the SDK (e.g. raw probes).
TRANSPORT_EXCEPTIONS: tuple = (
    LocalEndpointUnavailable,
    # Covers timeouts too: openai.APITimeoutError is a subclass of
    # APIConnectionError, so listing it separately would be redundant.
    openai.APIConnectionError,
    openai.InternalServerError,  # HTTP 5xx — endpoint up but failing
    httpx.ConnectError,
    httpx.TimeoutException,
)


# ---------------------------------------------------------------------------
# Startup probe
# ---------------------------------------------------------------------------

def probe_local_endpoint(
    *,
    base_url: str,
    model: str,
    api_key: Optional[str] = None,
    timeout: float = 10.0,
) -> None:
    """Eagerly verify a local OpenAI-compatible endpoint is alive.

    Two checks, mirroring what the daemons need at runtime:
      1. ``GET {root}/health`` — a llama-server CONVENTION (it serves /health
         at the server root, so a trailing ``/v1`` is stripped).  A 404 means
         "this server does not expose /health" (e.g. vLLM behind a path
         prefix, plain OpenAI-compatible proxies) — logged as a warning and
         tolerated, because check 2 proves liveness anyway.  Any other
         non-200 (5xx = up-but-failing, llama-server's 503 while the model
         loads) still fails the probe, as do all transport errors.
      2. ``POST {base}/chat/completions`` with ``max_tokens=1`` — proves the
         MODEL actually completes, not just that an HTTP server is listening.

    Raises:
        LocalEndpointUnavailable: on any transport error, a non-200/non-404
            health response, or a non-200 completion response — with the
            endpoint + model identity in the message.
    """
    base = (base_url or "").rstrip("/")
    if not base:
        raise LocalEndpointUnavailable(
            f"no base_url resolved for local LLM endpoint (model={model})"
        )
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    try:
        resp = requests.get(f"{root}/health", headers=headers, timeout=timeout)
        if resp.status_code == 404:
            log.warning(
                "GET /health returned 404 — endpoint does not expose "
                "llama-server's /health route; relying on the 1-token "
                "completion check for liveness: endpoint=%s model=%s",
                base, model,
            )
        elif resp.status_code != 200:
            raise LocalEndpointUnavailable(
                f"health check failed (HTTP {resp.status_code}): "
                f"endpoint={base} model={model}"
            )
        resp = requests.post(
            f"{base}/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            },
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise LocalEndpointUnavailable(
                f"1-token completion probe failed (HTTP {resp.status_code}): "
                f"endpoint={base} model={model}"
            )
    except (requests.RequestException, OSError) as e:
        # Narrow transport types only — anything else is a bug and propagates.
        raise LocalEndpointUnavailable(
            f"local LLM endpoint unreachable: endpoint={base} model={model} "
            f"({type(e).__name__}: {e})"
        ) from e


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

class AvailabilityCounter:
    """Failure counter for one daemon's LLM role — observable + persistent.

    In-memory fields (``consecutive``, ``total``, ``last_failure_ts``,
    ``last_reason``) drive the runtime fallback threshold; every failure is
    ALSO bumped into the ``ops_counters`` row named ``name`` so the soak
    report (Task 16) and the endpoint-down self-alert (Task 17) can read a
    restart-surviving total.

    Thread-safety: ``self._lock`` serializes THIS object's in-memory state
    and its own ``self._conn`` calls — it cannot serialize OTHER objects
    touching the same connection.  When the conn is SHARED with another
    holder (triage._main shares one ``check_same_thread=False`` conn with a
    DailyFallbackBudget), every holder must be constructed with the SAME
    ``lock``; with separate locks the C-level sqlite3 calls interleave,
    raising SystemError / sqlite3 errors and silently losing persisted
    bumps.  The default per-instance lock is safe only when this object is
    the connection's sole cross-thread user (the promoter: single-threaded,
    own conn).

    Self-alert seam (Task 17): when BOTH ``alert_threshold`` and
    ``on_threshold`` are given, the failure that brings ``consecutive`` up
    to the threshold invokes ``on_threshold(self)`` EXACTLY ONCE per outage:
    the debounce latch is set inside the lock (so a concurrent burst of
    failures cannot double-fire) and re-armed by ``record_success`` — the
    next outage alerts again.  The callback itself runs OUTSIDE the lock,
    AFTER it is released: the lock may be shared (triage) and the callback
    may do blocking transport I/O or even touch this counter again, neither
    of which may deadlock or stall other lock holders.  Callback exceptions
    are swallowed (logged) — the operator channel must never crash the
    daemon's loop.
    """

    def __init__(self, *, name: str, conn: Optional[sqlite3.Connection] = None,
                 lock: Optional[threading.Lock] = None,
                 alert_threshold: Optional[int] = None,
                 on_threshold: Optional[
                     Callable[["AvailabilityCounter"], None]] = None):
        self.name = name
        self._conn = conn
        self._lock = lock if lock is not None else threading.Lock()
        self.consecutive = 0
        self.total = 0
        self.last_failure_ts: Optional[str] = None
        self.last_reason: str = ""
        # Task 17: both must be provided to arm the seam (see class docstring).
        self.alert_threshold = (int(alert_threshold)
                                if alert_threshold is not None else None)
        self._on_threshold = on_threshold
        self._alerted_since_last_success = False

    def record_failure(self, reason: str = "") -> None:
        fire = False
        with self._lock:
            self.consecutive += 1
            self.total += 1
            self.last_failure_ts = datetime.now(timezone.utc).isoformat()
            self.last_reason = reason
            # Persist INSIDE the lock.  This serializes the conn only against
            # holders of THIS lock — which is why triage._main constructs the
            # counter and the budget sharing its conn with one shared lock.
            if self._conn is not None:
                try:
                    store.bump_ops_counter(self._conn, name=self.name)
                except sqlite3.Error:
                    log.exception("failed to persist ops counter %s", self.name)
            # Task 17 debounce: latch INSIDE the lock (single firer per
            # outage), fire OUTSIDE it.  ``>=`` not ``==`` so a crossing can
            # never be missed; the latch alone prevents repeats.
            if (self._on_threshold is not None
                    and self.alert_threshold is not None
                    and not self._alerted_since_last_success
                    and self.consecutive >= self.alert_threshold):
                self._alerted_since_last_success = True
                fire = True
        if fire:
            try:
                self._on_threshold(self)
            except Exception:  # noqa: BLE001 — alerting is best-effort
                log.exception(
                    "on_threshold self-alert callback failed for counter %s",
                    self.name)

    def record_success(self) -> None:
        """A successful LLM call: reset the consecutive run (total is monotonic)
        and re-arm the Task 17 self-alert latch (next outage alerts again)."""
        with self._lock:
            self.consecutive = 0
            self._alerted_since_last_success = False


class DailyFallbackBudget:
    """Hard per-UTC-day call budget for the ``fallback="api"`` path.

    Spend is persisted per day as ops_counter ``'<name>:<YYYY-MM-DD>'`` and
    re-read on the first consume of each day, so a daemon restart cannot
    reset an exhausted budget.

    Thread-safety: ``try_consume`` holds ``self._lock`` across the date
    observation and ALL of its own ``_conn`` reads/writes — but the lock
    cannot serialize OTHER objects touching the same connection.  As with
    AvailabilityCounter, callers sharing one conn between objects/threads
    (triage._main: record_failure on the event-loop thread, try_consume in
    ``asyncio.to_thread`` workers) must pass the SAME ``lock`` to every
    holder; the default per-instance lock is for sole-user conns only.
    """

    def __init__(self, *, name: str, max_per_day: int,
                 conn: Optional[sqlite3.Connection] = None,
                 lock: Optional[threading.Lock] = None):
        self.name = name
        self.max_per_day = int(max_per_day)
        self._conn = conn
        self._lock = lock if lock is not None else threading.Lock()
        self._date: Optional[str] = None
        self._spent = 0

    def _counter_name(self, day: str) -> str:
        return f"{self.name}:{day}"

    def try_consume(self) -> bool:
        """Consume one fallback call. False when today's budget is exhausted."""
        with self._lock:
            # Date observed INSIDE the lock so observations are monotonic
            # across the midnight rollover — an unlocked read could be
            # ordered after a competing thread's newer date and resurrect
            # yesterday's (already-rolled) spend.
            today = datetime.now(timezone.utc).date().isoformat()
            if self._date != today:
                self._date = today
                self._spent = 0
                if self._conn is not None:
                    try:
                        self._spent = store.get_ops_counter(
                            self._conn, name=self._counter_name(today))
                    except sqlite3.Error:
                        log.exception("failed to read ops counter %s",
                                      self._counter_name(today))
            if self._spent >= self.max_per_day:
                return False
            self._spent += 1
            if self._conn is not None:
                try:
                    store.bump_ops_counter(
                        self._conn, name=self._counter_name(today))
                except sqlite3.Error:
                    log.exception("failed to persist ops counter %s",
                                  self._counter_name(today))
            return True


# ---------------------------------------------------------------------------
# Role resolution with the availability policy
# ---------------------------------------------------------------------------

def strip_role_override(config: Dict[str, Any], role: str) -> Dict[str, Any]:
    """Config copy with ``role``'s override neutralized → GLOBAL resolution.

    provider/model/base_url are cleared so ``create_role_llm`` falls back to
    ``llm_provider`` / ``quick_think_llm`` / ``backend_url``.  ``extra_body``
    is also cleared: it carries local-server knobs (chat_template_kwargs)
    that must not be sent to API providers.  The input config is not mutated.
    """
    cfg = dict(config)
    roles = dict(cfg.get("llm_roles", {}))
    entry = dict(roles.get(role, {}))
    for key in ("provider", "model", "base_url", "extra_body"):
        entry[key] = None
    roles[role] = entry
    cfg["llm_roles"] = roles
    return cfg


def resolve_role_llm_global(role: str, config: Dict[str, Any]):
    """Second role resolution: the GLOBAL API provider (override stripped).

    Used for both the startup fallback (dead probe + fallback="api") and the
    runtime fallback (consecutive-failure threshold crossed).  Loudly logged.

    The classification fallback authenticates ONLY with the dedicated,
    removable ``IIC_LLM_FALLBACK_API_KEY`` — it never borrows the worker's
    global provider key (e.g. ``DEEPSEEK_API_KEY``).  Absent that key the
    fallback is unavailable: we raise rather than silently sharing a credential
    (structural isolation, design 2026-06-13).

    Resolution strips the role override, so the fallback uses the GLOBAL
    ``llm_provider``/``quick_think_llm`` with ``IIC_LLM_FALLBACK_API_KEY`` as
    that provider's credential. (If the global provider is itself ``local``,
    the dedicated key is sent as the local server's auth header instead of
    ``LOCAL_LLM_API_KEY`` — use per-role overrides, not a global ``local``
    provider, if that matters.)
    """
    fallback_key = os.environ.get("IIC_LLM_FALLBACK_API_KEY")
    if not fallback_key:
        log.error(
            "role %s: fallback=api engaged but IIC_LLM_FALLBACK_API_KEY is not "
            "set; refusing to engage the cloud fallback (it never borrows the "
            "worker key)", role,
        )
        raise LocalEndpointUnavailable(
            f"role {role}: fallback=api engaged but IIC_LLM_FALLBACK_API_KEY "
            f"is not set; refusing to borrow the global provider key"
        )
    # Module-attribute access (not from-import) so tests that patch
    # ``factory.create_role_llm`` intercept this call too.
    from tradingagents.llm_clients import factory
    client = factory.create_role_llm(
        role, strip_role_override(config, role), api_key=fallback_key)
    # getattr-defensive: tests patch create_role_llm with minimal fakes that
    # may lack base_url/get_provider_name.
    provider_name = getattr(client, "get_provider_name", lambda: "?")()
    log.warning(
        "role %s re-resolved to GLOBAL provider (fallback=api): "
        "provider=%s model=%s base_url=%s",
        role, provider_name, client.model, getattr(client, "base_url", None),
    )
    return client


def resolve_role_llm_with_fallback(
    role: str,
    config: Dict[str, Any],
    *,
    probe=None,
) -> Tuple[Any, bool]:
    """Resolve ``role`` to an LLM client, applying the D5 startup policy.

    1. Resolve via ``create_role_llm`` and log the resolved endpoint + model
       identity (the L3 gate requires this in startup logs).
    2. ONLY when the resolved provider is ``local``: run the eager probe.
       - probe OK            → return ``(client, False)``;
       - probe fails, ``fallback="none"`` (default) → re-raise — the daemon
         REFUSES to start (degrade loudly, fail fast at startup);
       - probe fails, ``fallback="api"`` → return the global re-resolution
         ``(client, True)``; the caller must bound calls with a
         ``DailyFallbackBudget``.

    Raises:
        LocalEndpointUnavailable: dead local endpoint with fallback="none".
    """
    from tradingagents.llm_clients import factory
    client = factory.create_role_llm(role, config)

    override: Dict[str, Any] = config.get("llm_roles", {}).get(role, {}) or {}
    provider = (override.get("provider") or config.get("llm_provider") or "")
    provider = provider.lower()
    fallback = (override.get("fallback") or "none").lower()

    # getattr-defensive: tests patch create_role_llm with minimal fakes that
    # may lack base_url.
    base_url = getattr(client, "base_url", None)
    if provider == "local" and not base_url:
        # Mirror the client's call-time default (incl. LOCAL_LLM_BASE_URL).
        from tradingagents.llm_clients.openai_client import (
            _resolve_provider_base_url,
        )
        base_url = _resolve_provider_base_url("local")

    log.info(
        "role %s resolved: provider=%s model=%s base_url=%s fallback=%s",
        role, provider, client.model, base_url, fallback,
    )
    if provider != "local":
        return client, False

    probe_fn = probe or probe_local_endpoint
    try:
        probe_fn(base_url=base_url, model=client.model,
                 api_key=os.environ.get("LOCAL_LLM_API_KEY"))
    except LocalEndpointUnavailable as e:
        if fallback != "api":
            log.error(
                "role %s startup probe FAILED and fallback=%s — refusing to "
                "start: %s", role, fallback, e,
            )
            raise
        log.error(
            "role %s startup probe FAILED — falling back to the global API "
            "provider (fallback=api): %s", role, e,
        )
        return resolve_role_llm_global(role, config), True

    log.info("role %s startup probe OK: endpoint=%s model=%s",
             role, base_url, client.model)
    return client, False


def warn_if_fallback_unsatisfiable(
    role: str, fallback_mode: Optional[str], max_per_day: float, *,
    fallback_key_present: bool, log: logging.Logger,
) -> None:
    """Loudly warn at startup when ``fallback=api`` can never actually fire.

    Two unsatisfiable configs are surfaced (either or both):
      - the per-UTC-day budget is non-positive (every fallback call is denied);
      - the dedicated ``IIC_LLM_FALLBACK_API_KEY`` is absent (the fallback
        refuses rather than borrowing the worker key).

    Every other combination — including the fail-closed production default
    (``fallback="none"``) — is a silent no-op.
    """
    if (fallback_mode or "none").strip().lower() != "api":
        return
    if max_per_day <= 0:
        log.warning(
            "role %s: fallback=api but daily budget is %s (<=0); the fallback "
            "will NEVER fire. Set IIC_LLM_FALLBACK_DAILY_BUDGET > 0 to enable it.",
            role, max_per_day,
        )
    if not fallback_key_present:
        log.warning(
            "role %s: fallback=api but IIC_LLM_FALLBACK_API_KEY is not set; the "
            "fallback will REFUSE (it never borrows the worker key). Set "
            "IIC_LLM_FALLBACK_API_KEY to enable it.",
            role,
        )
