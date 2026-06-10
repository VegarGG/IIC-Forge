"""Tests for the local-vs-API call-volume split in the costs panel.

Seeding strategy
----------------
* ``provider='local', usd_estimate=0.0``   → FREE  (real zero, must be counted)
* ``provider='deepseek', usd_estimate=0.0012`` → API paid call
* ``provider='deepseek', usd_estimate=NULL``   → UNKNOWN (excluded from free tally)

The panel must:
- Report separate call counts for 'local' and non-local providers.
- Treat ``usd_estimate=0.0`` as FREE (counted in free_calls).
- Treat ``usd_estimate IS NULL`` as UNKNOWN (excluded from free_calls and from
  api_spend sums; must not be coalesced to 0.0 anywhere).
"""

from __future__ import annotations

import pytest

from tradingagents.persistence.db import connect as iic_connect
from tradingagents.persistence import store


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _seed_run(conn, run_id: str, ts: str = "2026-06-09T10:00:00+00:00") -> None:
    store.insert_run(
        conn,
        run_id=run_id,
        ticker="AAPL",
        persona_id="macro",
        started_ts=ts,
        artifact_dir=f"runs/{run_id}",
    )
    store.finalize_run(
        conn, run_id=run_id, ended_ts=ts, status="complete",
        decision="BUY", confidence=0.7,
    )


def _seed_cost(conn, run_id: str, provider: str, usd_estimate, model: str = "m") -> None:
    """Insert a cost row; usd_estimate may be None (NULL) or a float."""
    store.record_cost(
        conn,
        run_id=run_id,
        provider=provider,
        model=model,
        in_tokens=100,
        out_tokens=50,
        usd_estimate=usd_estimate,
    )


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_split_counts_local_calls_as_free(tmp_path):
    """local provider with usd_estimate=0.0 appears in free_calls count."""
    from tradingagents.dashboard.panels.costs import fetch_provider_split

    conn = iic_connect(str(tmp_path / "iic.db"))
    _seed_run(conn, "r1")
    _seed_cost(conn, "r1", provider="local", usd_estimate=0.0)

    result = fetch_provider_split(conn)
    assert result["local_calls"] == 1
    assert result["free_calls"] == 1


@pytest.mark.unit
def test_split_counts_api_calls_separately(tmp_path):
    """deepseek rows with a non-zero usd appear in api_calls and api_spend."""
    from tradingagents.dashboard.panels.costs import fetch_provider_split

    conn = iic_connect(str(tmp_path / "iic.db"))
    _seed_run(conn, "r1")
    _seed_cost(conn, "r1", provider="deepseek", usd_estimate=0.0012)

    result = fetch_provider_split(conn)
    assert result["api_calls"] == 1
    assert abs(result["api_spend"] - 0.0012) < 1e-9
    assert result["local_calls"] == 0
    assert result["free_calls"] == 0


@pytest.mark.unit
def test_split_null_usd_is_unknown_not_free(tmp_path):
    """NULL usd_estimate is excluded from free_calls and from api_spend."""
    from tradingagents.dashboard.panels.costs import fetch_provider_split

    conn = iic_connect(str(tmp_path / "iic.db"))
    _seed_run(conn, "r1")
    _seed_cost(conn, "r1", provider="deepseek", usd_estimate=None)

    result = fetch_provider_split(conn)
    # NULL means unknown: not counted as free, not added to api_spend
    assert result["free_calls"] == 0
    assert result["unknown_calls"] == 1
    assert result["api_spend"] == pytest.approx(0.0)


@pytest.mark.unit
def test_split_mixed_seed(tmp_path):
    """Mixed rows: 1 local/free, 1 API/paid, 1 API/unknown — all tallied correctly."""
    from tradingagents.dashboard.panels.costs import fetch_provider_split

    conn = iic_connect(str(tmp_path / "iic.db"))
    _seed_run(conn, "r1")
    _seed_run(conn, "r2")
    _seed_run(conn, "r3")

    _seed_cost(conn, "r1", provider="local",    usd_estimate=0.0)       # free
    _seed_cost(conn, "r2", provider="deepseek", usd_estimate=0.0012)    # paid API
    _seed_cost(conn, "r3", provider="deepseek", usd_estimate=None)      # unknown

    result = fetch_provider_split(conn)

    assert result["local_calls"]   == 1
    assert result["api_calls"]     == 2   # deepseek rows regardless of usd
    assert result["free_calls"]    == 1   # only the explicit 0.0 row
    assert result["unknown_calls"] == 1   # NULL row
    assert result["api_spend"]     == pytest.approx(0.0012)


@pytest.mark.unit
def test_split_zero_usd_api_row_is_free_not_unknown(tmp_path):
    """An API row with usd_estimate=0.0 is FREE (real zero), not UNKNOWN (NULL)."""
    from tradingagents.dashboard.panels.costs import fetch_provider_split

    conn = iic_connect(str(tmp_path / "iic.db"))
    _seed_run(conn, "r1")
    # provider is not 'local' but usd is exactly 0.0 — still counts as free
    _seed_cost(conn, "r1", provider="deepseek", usd_estimate=0.0)

    result = fetch_provider_split(conn)
    assert result["free_calls"]    == 1
    assert result["unknown_calls"] == 0
    assert result["api_spend"]     == pytest.approx(0.0)
