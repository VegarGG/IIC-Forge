from collections import Counter

from scripts.quality_ratchet import parse_mypy, parse_ruff, regressions


def test_parse_ruff_ignores_line_numbers() -> None:
    output = """[
      {"filename":"tradingagents/example.py","code":"F401","message":"unused","location":{"row":9,"column":1}},
      {"filename":"tradingagents/example.py","code":"F401","message":"unused","location":{"row":99,"column":1}}
    ]"""
    assert parse_ruff(output) == Counter(
        {("tradingagents/example.py", "F401", "unused"): 2}
    )


def test_parse_mypy_ignores_positions_and_summary() -> None:
    output = "\n".join(
        (
            'cli/example.py:10:5: error: Incompatible types [assignment]',
            'cli/example.py:90: error: Incompatible types  [assignment]',
            "Found 2 errors in 1 file (checked 1 source file)",
        )
    )
    assert parse_mypy(output) == Counter(
        {("cli/example.py", "assignment", "Incompatible types"): 2}
    )


def test_regressions_allows_reductions_but_rejects_new_or_increased() -> None:
    old = ("old.py", "F401", "unused")
    new = ("new.py", "F841", "unused local")
    baseline = Counter({old: 2})
    assert regressions(baseline, Counter({old: 1})) == []
    assert regressions(baseline, Counter({old: 3, new: 1})) == [
        (new, 0, 1),
        (old, 2, 3),
    ]
