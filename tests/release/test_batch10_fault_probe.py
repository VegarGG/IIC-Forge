from scripts.batch10_fault_probe import budget_probe, quiet_hours_probe


def test_budget_probe_blocks_call_21_at_usd_20(tmp_path) -> None:
    result = budget_probe(tmp_path / "budget.db")
    assert result == {
        "status": "passed",
        "limit_usd": 20,
        "reserved_usd": 20.0,
        "next_call_blocked": True,
    }


def test_quiet_hours_probe_releases_at_0700_beijing(tmp_path) -> None:
    result = quiet_hours_probe(tmp_path / "quiet.db")
    assert result["status"] == "passed"
    assert result["timezone"] == "Asia/Shanghai"
    assert result["available_utc"] == "2026-08-09T23:00:00+00:00"
