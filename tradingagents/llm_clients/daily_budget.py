"""Atomic, process-wide paid-LLM budget for one Beijing calendar day."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from tradingagents.llm_clients.pricing import estimate_usd, pricing_for


log = logging.getLogger(__name__)


class DailyBudgetExceeded(RuntimeError):
    """The next paid call cannot fit under the configured daily ceiling."""


class BudgetConfigurationError(RuntimeError):
    """The budget cannot safely price or reserve the configured paid model."""


def beijing_budget_date(
    now: datetime | None = None, *, timezone_name: str = "Asia/Shanghai"
) -> str:
    zone = ZoneInfo(timezone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("budget clock must be timezone-aware")
    return current.astimezone(zone).date().isoformat()


def _charged_expression() -> str:
    return (
        "CASE WHEN state = 'reserved' THEN reserved_usd "
        "ELSE COALESCE(actual_usd, reserved_usd) END"
    )


def daily_budget_total(
    conn: sqlite3.Connection,
    *,
    budget_date: str,
) -> float:
    row = conn.execute(
        f"SELECT COALESCE(SUM(MAX(0.0, {_charged_expression()} - "
        "COALESCE(r.released_usd, 0.0))), 0.0) "
        "FROM llm_budget_ledger l LEFT JOIN llm_budget_releases r "
        "ON r.call_id=l.call_id WHERE l.budget_date = ?",
        (budget_date,),
    ).fetchone()
    return float(row[0] or 0.0)


def release_stale_reservation(
    conn: sqlite3.Connection,
    *,
    call_id: str,
    operator_note: str,
    evidence: str,
    confirm: str,
    minimum_age_hours: int = 1,
    now: datetime | None = None,
) -> float:
    """Append an audited release for a provably abandoned reservation.

    The original ledger row is never changed. Settled/charged calls and recent
    reservations cannot be released, and the primary key prevents a second
    release from reducing the budget twice.
    """
    if confirm != f"RELEASE {call_id}":
        raise ValueError(f"confirmation must be exactly 'RELEASE {call_id}'")
    if not operator_note.strip() or not evidence.strip():
        raise ValueError("operator note and evidence must not be empty")
    if minimum_age_hours < 1:
        raise ValueError("minimum reservation age must be at least one hour")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with conn:
        row = conn.execute(
            "SELECT state, reserved_usd, created_ts FROM llm_budget_ledger "
            "WHERE call_id=?",
            (call_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"budget call {call_id!r} does not exist")
        if row["state"] != "reserved":
            raise ValueError("only a reserved budget call can be released")
        if conn.execute(
            "SELECT 1 FROM llm_budget_releases WHERE call_id=?", (call_id,)
        ).fetchone() is not None:
            raise ValueError("budget reservation was already released")
        created = datetime.fromisoformat(str(row["created_ts"]))
        if created.tzinfo is None:
            raise ValueError("reservation timestamp is not timezone-aware")
        if current - created.astimezone(timezone.utc) < timedelta(
            hours=minimum_age_hours
        ):
            raise ValueError(
                f"reservation must be at least {minimum_age_hours} hour(s) old"
            )
        released = float(row["reserved_usd"])
        conn.execute(
            "INSERT INTO llm_budget_releases (call_id, released_usd, released_ts, "
            "operator_note, evidence) VALUES (?, ?, ?, ?, ?)",
            (
                call_id,
                released,
                current.isoformat(),
                operator_note.strip()[:2000],
                evidence.strip()[:4000],
            ),
        )
        conn.execute(
            "INSERT INTO operator_actions (action_type, target_type, target_id, "
            "requested_ts, operator_note, result, metadata) "
            "VALUES ('release_budget_reservation', 'llm_budget_call', ?, ?, ?, "
            "'released', ?)",
            (
                call_id,
                current.isoformat(),
                operator_note.strip()[:2000],
                json.dumps({"released_usd": released}, sort_keys=True),
            ),
        )
    return released


class DailyUsdBudget:
    """SQLite-backed reservation/reconciliation ledger shared by processes."""

    def __init__(
        self,
        *,
        db_path: str,
        provider: str,
        model: str,
        daily_limit_usd: float,
        reservation_usd: float,
        timezone_name: str,
    ) -> None:
        self.db_path = str(Path(db_path).expanduser())
        self.provider = provider.strip().lower()
        self.model = model.strip().lower()
        self.daily_limit_usd = float(daily_limit_usd)
        self.reservation_usd = float(reservation_usd)
        self.timezone_name = timezone_name
        pricing = pricing_for(self.provider, self.model)
        if pricing is None:
            raise BudgetConfigurationError(
                f"no production price schedule for {self.provider}/{self.model}"
            )
        if self.daily_limit_usd <= 0:
            raise BudgetConfigurationError("daily LLM budget must be positive")
        if self.reservation_usd < pricing.conservative_maximum_request_usd:
            raise BudgetConfigurationError(
                f"reservation ${self.reservation_usd:.4f} is below the "
                f"conservative maximum ${pricing.conservative_maximum_request_usd:.4f} "
                f"for {self.model}"
            )
        ZoneInfo(self.timezone_name)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def reserve(self, call_id: str, *, now: datetime | None = None) -> None:
        budget_date = beijing_budget_date(now, timezone_name=self.timezone_name)
        created_ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT budget_date, provider, model, state "
                "FROM llm_budget_ledger WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            if existing is not None:
                same_active_call = (
                    existing[0] == budget_date
                    and existing[1] == self.provider
                    and existing[2] == self.model
                    and existing[3] == "reserved"
                )
                if same_active_call:
                    conn.commit()
                    return
                conn.rollback()
                raise BudgetConfigurationError(
                    "LLM budget call ID cannot be reused after settlement or "
                    "across provider, model, or Beijing-day boundaries"
                )
            total = daily_budget_total(conn, budget_date=budget_date)
            if total + self.reservation_usd > self.daily_limit_usd + 1e-9:
                conn.rollback()
                raise DailyBudgetExceeded(
                    f"Beijing-day LLM budget exhausted: charged/reserved "
                    f"${total:.6f} + next ${self.reservation_usd:.6f} > "
                    f"${self.daily_limit_usd:.2f}"
                )
            conn.execute(
                "INSERT INTO llm_budget_ledger ("
                "call_id, budget_date, budget_timezone, provider, model, state, "
                "reserved_usd, created_ts) VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)",
                (
                    call_id,
                    budget_date,
                    self.timezone_name,
                    self.provider,
                    self.model,
                    self.reservation_usd,
                    created_ts,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def settle(
        self,
        call_id: str,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        now: datetime | None = None,
    ) -> None:
        actual = estimate_usd(
            self.provider,
            self.model,
            in_tokens=prompt_tokens,
            out_tokens=completion_tokens,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
        )
        if actual is None:
            raise BudgetConfigurationError(
                f"cannot settle unpriced model {self.provider}/{self.model}"
            )
        if actual > self.reservation_usd + 1e-9:
            raise BudgetConfigurationError(
                f"reported cost ${actual:.6f} exceeded the pre-call reservation "
                f"${self.reservation_usd:.6f}"
            )
        settled_ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE llm_budget_ledger SET state='settled', actual_usd=?, "
                    "prompt_tokens=?, completion_tokens=?, cache_hit_tokens=?, "
                    "cache_miss_tokens=?, settled_ts=?, error=NULL "
                    "WHERE call_id=? AND state='reserved'",
                    (
                        actual,
                        max(0, int(prompt_tokens)),
                        max(0, int(completion_tokens)),
                        max(0, int(cache_hit_tokens)),
                        max(0, int(cache_miss_tokens)),
                        settled_ts,
                        call_id,
                    ),
                )
        finally:
            conn.close()

    def charge_reservation(
        self, call_id: str, *, error: str, now: datetime | None = None
    ) -> None:
        settled_ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        safe_error = str(error).replace("\x00", "")[:500]
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE llm_budget_ledger SET state='charged', "
                    "actual_usd=reserved_usd, settled_ts=?, error=? "
                    "WHERE call_id=? AND state='reserved'",
                    (settled_ts, safe_error, call_id),
                )
        finally:
            conn.close()


def _usage_from_response(response: LLMResult) -> tuple[int, int, int, int] | None:
    info = response.llm_output or {}
    usage = info.get("token_usage") or info.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(
        usage.get("completion_tokens") or usage.get("output_tokens") or 0
    )
    hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    if prompt or completion or hit or miss:
        return prompt, completion, hit, miss
    for generations in response.generations or []:
        for generation in generations:
            metadata = getattr(getattr(generation, "message", None), "usage_metadata", None)
            if not metadata:
                continue
            prompt = int(metadata.get("input_tokens") or 0)
            completion = int(metadata.get("output_tokens") or 0)
            details = metadata.get("input_token_details") or {}
            hit = int(details.get("cache_read") or details.get("cache_hit") or 0)
            miss = max(0, prompt - hit)
            return prompt, completion, hit, miss
    return None


class DailyUsdBudgetCallback(BaseCallbackHandler):
    """LangChain callback that fences every underlying provider request."""

    raise_error = True

    def __init__(self, budget: DailyUsdBudget) -> None:
        super().__init__()
        self._budget = budget

    def _reserve(self, run_id: Any) -> None:
        self._budget.reserve(str(run_id))

    def on_llm_start(self, serialized: dict[str, Any], prompts: list[str], *, run_id: Any, **kwargs: Any) -> None:
        self._reserve(run_id)

    def on_chat_model_start(self, serialized: dict[str, Any], messages: list[list[Any]], *, run_id: Any, **kwargs: Any) -> None:
        self._reserve(run_id)

    def on_llm_end(self, response: LLMResult, *, run_id: Any, **kwargs: Any) -> None:
        usage = _usage_from_response(response)
        if usage is None:
            self._budget.charge_reservation(
                str(run_id), error="provider response omitted token usage"
            )
            log.error("LLM usage missing; full reservation charged for run %s", run_id)
            return
        self._budget.settle(
            str(run_id),
            prompt_tokens=usage[0],
            completion_tokens=usage[1],
            cache_hit_tokens=usage[2],
            cache_miss_tokens=usage[3],
        )

    def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        self._budget.charge_reservation(str(run_id), error=type(error).__name__)


def callback_from_config(
    *, provider: str, model: str, config: Mapping[str, Any] | None
) -> DailyUsdBudgetCallback | None:
    """Build the enforcement callback when the caller supplies an enabled config."""
    if not config or not bool(config.get("daily_budget_enabled")):
        return None
    normalized_provider = provider.strip().lower()
    if normalized_provider in {"local", "ollama"}:
        return None
    budget = DailyUsdBudget(
        db_path=str(config["iic_db_path"]),
        provider=normalized_provider,
        model=model,
        daily_limit_usd=float(config["daily_budget_usd"]),
        reservation_usd=float(config["daily_budget_reservation_usd"]),
        timezone_name=str(config["daily_budget_timezone"]),
    )
    return DailyUsdBudgetCallback(budget)


def append_budget_callback(
    kwargs: dict[str, Any], *, provider: str, model: str, config: Mapping[str, Any] | None
) -> dict[str, Any]:
    callback = callback_from_config(provider=provider, model=model, config=config)
    if callback is None:
        return kwargs
    updated = dict(kwargs)
    # One reservation authorizes one provider request. SDK-level retries can
    # create additional billable requests without another callback start, so
    # paid clients must surface failure to the application. A caller may retry
    # only through a new LangChain run, which obtains a fresh reservation.
    updated["max_retries"] = 0
    existing = updated.get("callbacks")
    if existing is None:
        updated["callbacks"] = [callback]
    elif isinstance(existing, (list, tuple)):
        updated["callbacks"] = [*existing, callback]
    else:
        updated["callbacks"] = [existing, callback]
    return updated
