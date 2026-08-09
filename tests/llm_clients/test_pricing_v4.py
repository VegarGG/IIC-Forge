import pytest


@pytest.mark.unit
def test_official_v4_price_schedule_and_maximum_fit_one_dollar_reservation():
    from tradingagents.llm_clients.pricing import DEEPSEEK_V4_PRICING, estimate_usd

    flash = DEEPSEEK_V4_PRICING["deepseek-v4-flash"]
    pro = DEEPSEEK_V4_PRICING["deepseek-v4-pro"]
    assert flash.conservative_maximum_request_usd == pytest.approx(0.24752)
    assert pro.conservative_maximum_request_usd == pytest.approx(0.76908)
    assert pro.conservative_maximum_request_usd < 1.0
    assert estimate_usd(
        "deepseek",
        "deepseek-v4-pro",
        in_tokens=1_000_000,
        out_tokens=384_000,
        cache_miss_tokens=1_000_000,
    ) == pytest.approx(0.76908)


@pytest.mark.unit
def test_unknown_model_is_never_silently_priced_as_cheaper_alias():
    from tradingagents.llm_clients.pricing import estimate_usd

    assert estimate_usd(
        "deepseek", "deepseek-future", in_tokens=1, out_tokens=1
    ) is None
