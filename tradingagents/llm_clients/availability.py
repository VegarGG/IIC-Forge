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
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

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
    openai.APIConnectionError,   # superclass of openai.APITimeoutError
    openai.APITimeoutError,
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
      1. ``GET {root}/health``      — llama-server serves /health at the
         server root, so a trailing ``/v1`` is stripped for this check;
      2. ``POST {base}/chat/completions`` with ``max_tokens=1`` — proves the
         MODEL actually completes, not just that an HTTP server is listening.

    Raises:
        LocalEndpointUnavailable: on any transport error or non-200 response,
            with the endpoint + model identity in the message.
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
        if resp.status_code != 200:
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
    restart-surviving total.  Thread-safe (a lock guards the in-memory state;
    sqlite serializes its own writes).
    """

    def __init__(self, *, name: str, conn: Optional[sqlite3.Connection] = None):
        self.name = name
        self._conn = conn
        self._lock = threading.Lock()
        self.consecutive = 0
        self.total = 0
        self.last_failure_ts: Optional[str] = None
        self.last_reason: str = ""

    def record_failure(self, reason: str = "") -> None:
        with self._lock:
            self.consecutive += 1
            self.total += 1
            self.last_failure_ts = datetime.now(timezone.utc).isoformat()
            self.last_reason = reason
        if self._conn is not None:
            try:
                store.bump_ops_counter(self._conn, name=self.name)
            except sqlite3.Error:
                log.exception("failed to persist ops counter %s", self.name)

    def record_success(self) -> None:
        """A successful LLM call: reset the consecutive run (total is monotonic)."""
        with self._lock:
            self.consecutive = 0


class DailyFallbackBudget:
    """Hard per-UTC-day call budget for the ``fallback="api"`` path.

    Spend is persisted per day as ops_counter ``'<name>:<YYYY-MM-DD>'`` and
    re-read on the first consume of each day, so a daemon restart cannot
    reset an exhausted budget.
    """

    def __init__(self, *, name: str, max_per_day: int,
                 conn: Optional[sqlite3.Connection] = None):
        self.name = name
        self.max_per_day = int(max_per_day)
        self._conn = conn
        self._lock = threading.Lock()
        self._date: Optional[str] = None
        self._spent = 0

    def _counter_name(self, day: str) -> str:
        return f"{self.name}:{day}"

    def try_consume(self) -> bool:
        """Consume one fallback call. False when today's budget is exhausted."""
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
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
    """
    # Module-attribute access (not from-import) so tests that patch
    # ``factory.create_role_llm`` intercept this call too.
    from tradingagents.llm_clients import factory
    client = factory.create_role_llm(role, strip_role_override(config, role))
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
