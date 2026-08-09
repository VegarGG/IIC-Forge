from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from tradingagents.persistence.db import connect


def _budget(path, *, limit=20.0, reservation=1.0, model="deepseek-v4-pro"):
    from tradingagents.llm_clients.daily_budget import DailyUsdBudget

    return DailyUsdBudget(
        db_path=str(path),
        provider="deepseek",
        model=model,
        daily_limit_usd=limit,
        reservation_usd=reservation,
        timezone_name="Asia/Shanghai",
    )


@pytest.mark.unit
def test_beijing_budget_date_rolls_at_1600_utc():
    from tradingagents.llm_clients.daily_budget import beijing_budget_date

    before = datetime(2026, 8, 9, 15, 59, 59, tzinfo=timezone.utc)
    after = datetime(2026, 8, 9, 16, 0, 0, tzinfo=timezone.utc)
    assert beijing_budget_date(before) == "2026-08-09"
    assert beijing_budget_date(after) == "2026-08-10"


@pytest.mark.unit
def test_reservations_are_atomic_across_process_style_connections(tmp_path):
    from tradingagents.llm_clients.daily_budget import DailyBudgetExceeded

    path = tmp_path / "iic.db"
    connect(str(path)).close()

    def attempt(index: int) -> bool:
        try:
            _budget(path, limit=2.0).reserve(f"call-{index}")
            return True
        except DailyBudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=4) as executor:
        outcomes = list(executor.map(attempt, range(4)))

    assert sum(outcomes) == 2
    conn = connect(str(path))
    assert conn.execute("SELECT COUNT(*) FROM llm_budget_ledger").fetchone()[0] == 2
    assert conn.execute(
        "SELECT SUM(reserved_usd) FROM llm_budget_ledger"
    ).fetchone()[0] == pytest.approx(2.0)


@pytest.mark.unit
def test_success_reconciles_reservation_to_reported_usage(tmp_path):
    from tradingagents.llm_clients.daily_budget import (
        beijing_budget_date,
        daily_budget_total,
    )

    path = tmp_path / "iic.db"
    connect(str(path)).close()
    budget = _budget(path)
    budget.reserve("call-1")
    budget.settle(
        "call-1",
        prompt_tokens=10_000,
        completion_tokens=2_000,
        cache_hit_tokens=2_000,
        cache_miss_tokens=8_000,
    )

    conn = connect(str(path))
    row = conn.execute(
        "SELECT state, reserved_usd, actual_usd, prompt_tokens, "
        "completion_tokens FROM llm_budget_ledger WHERE call_id='call-1'"
    ).fetchone()
    assert row["state"] == "settled"
    assert row["reserved_usd"] == 1.0
    assert 0 < row["actual_usd"] < 1.0
    assert row["prompt_tokens"] == 10_000
    assert row["completion_tokens"] == 2_000
    assert daily_budget_total(
        conn, budget_date=beijing_budget_date()
    ) == pytest.approx(row["actual_usd"])


@pytest.mark.unit
def test_settled_call_id_cannot_authorize_another_request(tmp_path):
    from tradingagents.llm_clients.daily_budget import BudgetConfigurationError

    path = tmp_path / "iic.db"
    connect(str(path)).close()
    budget = _budget(path)
    budget.reserve("single-use")
    budget.settle("single-use", prompt_tokens=100, completion_tokens=25)
    with pytest.raises(BudgetConfigurationError, match="cannot be reused"):
        budget.reserve("single-use")


@pytest.mark.unit
def test_missing_usage_charges_full_reservation(tmp_path):
    from tradingagents.llm_clients.daily_budget import DailyUsdBudgetCallback

    path = tmp_path / "iic.db"
    connect(str(path)).close()
    callback = DailyUsdBudgetCallback(_budget(path))
    callback.on_chat_model_start({}, [[]], run_id="missing-usage")
    callback.on_llm_end(
        LLMResult(
            generations=[[ChatGeneration(message=AIMessage(content="done"))]],
            llm_output={},
        ),
        run_id="missing-usage",
    )

    conn = connect(str(path))
    row = conn.execute(
        "SELECT state, actual_usd, error FROM llm_budget_ledger "
        "WHERE call_id='missing-usage'"
    ).fetchone()
    assert tuple(row) == (
        "charged",
        1.0,
        "provider response omitted token usage",
    )


@pytest.mark.unit
def test_callback_settles_deepseek_cache_usage(tmp_path):
    from tradingagents.llm_clients.daily_budget import DailyUsdBudgetCallback

    path = tmp_path / "iic.db"
    connect(str(path)).close()
    callback = DailyUsdBudgetCallback(_budget(path, model="deepseek-v4-flash"))
    callback.on_chat_model_start({}, [[]], run_id="usage-call")
    result = LLMResult(
        generations=[[ChatGeneration(message=AIMessage(content="done"))]],
        llm_output={
            "token_usage": {
                "prompt_tokens": 100,
                "completion_tokens": 25,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
            }
        },
    )
    callback.on_llm_end(result, run_id="usage-call")

    conn = connect(str(path))
    row = conn.execute(
        "SELECT state, cache_hit_tokens, cache_miss_tokens, actual_usd "
        "FROM llm_budget_ledger WHERE call_id='usage-call'"
    ).fetchone()
    assert row["state"] == "settled"
    assert row["cache_hit_tokens"] == 80
    assert row["cache_miss_tokens"] == 20
    assert row["actual_usd"] > 0


@pytest.mark.unit
def test_unpriced_paid_model_and_undersized_reservation_fail_closed(tmp_path):
    from tradingagents.llm_clients.daily_budget import (
        BudgetConfigurationError,
        DailyUsdBudget,
    )

    path = tmp_path / "iic.db"
    connect(str(path)).close()
    with pytest.raises(BudgetConfigurationError, match="no production price"):
        DailyUsdBudget(
            db_path=str(path),
            provider="openai",
            model="gpt-5",
            daily_limit_usd=20,
            reservation_usd=1,
            timezone_name="Asia/Shanghai",
        )
    with pytest.raises(BudgetConfigurationError, match="below the conservative"):
        _budget(path, reservation=0.5)


@pytest.mark.unit
def test_factory_attaches_budget_only_to_paid_production_client(tmp_path):
    from tradingagents.llm_clients.daily_budget import DailyUsdBudgetCallback
    from tradingagents.llm_clients.factory import create_llm_client

    config = {
        "daily_budget_enabled": True,
        "daily_budget_usd": 20.0,
        "daily_budget_timezone": "Asia/Shanghai",
        "daily_budget_reservation_usd": 1.0,
        "iic_db_path": str(tmp_path / "iic.db"),
    }
    paid = create_llm_client(
        "deepseek", "deepseek-v4-pro", budget_config=config
    )
    assert any(
        isinstance(callback, DailyUsdBudgetCallback)
        for callback in paid.kwargs["callbacks"]
    )
    assert paid.kwargs["max_retries"] == 0
    local = create_llm_client(
        "local", "qwen-local", budget_config=config
    )
    assert "callbacks" not in local.kwargs
    assert "max_retries" not in local.kwargs


@pytest.mark.unit
def test_langchain_propagates_pre_call_budget_denial(tmp_path):
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from tradingagents.llm_clients.daily_budget import (
        DailyBudgetExceeded,
        DailyUsdBudgetCallback,
    )

    path = tmp_path / "iic.db"
    connect(str(path)).close()
    callback = DailyUsdBudgetCallback(_budget(path, limit=2.0))
    model = FakeListChatModel(
        responses=["first", "second", "must-not-run"],
        callbacks=[callback],
    )
    model.invoke("one")
    model.invoke("two")
    with pytest.raises(DailyBudgetExceeded, match="budget exhausted"):
        model.invoke("three")
