"""Auditable DeepSeek V4 pricing used by telemetry and budget enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ModelPricing:
    input_cache_hit_per_million: float
    input_cache_miss_per_million: float
    output_per_million: float
    context_tokens: int
    maximum_output_tokens: int

    @property
    def conservative_maximum_request_usd(self) -> float:
        # Deliberately assumes a full context of cache misses PLUS the full
        # advertised output maximum.  The real combined context constraint is
        # tighter, so this is an upper bound rather than an expected charge.
        return (
            self.context_tokens * self.input_cache_miss_per_million
            + self.maximum_output_tokens * self.output_per_million
        ) / 1_000_000


# Official DeepSeek API pricing checked 2026-08-09:
# https://api-docs.deepseek.com/quick_start/pricing
DEEPSEEK_V4_PRICING = {
    "deepseek-v4-flash": ModelPricing(0.0028, 0.14, 0.28, 1_000_000, 384_000),
    "deepseek-v4-pro": ModelPricing(0.003625, 0.435, 0.87, 1_000_000, 384_000),
}


def pricing_for(provider: str, model: str) -> Optional[ModelPricing]:
    if (provider or "").strip().lower() != "deepseek":
        return None
    return DEEPSEEK_V4_PRICING.get((model or "").strip().lower())


def estimate_usd(
    provider: str,
    model: str,
    *,
    in_tokens: int,
    out_tokens: int,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int = 0,
) -> Optional[float]:
    """Return the current price-schedule estimate, or None if unpriced."""
    pricing = pricing_for(provider, model)
    if pricing is None:
        return None
    hit = max(0, int(cache_hit_tokens or 0))
    miss = max(0, int(cache_miss_tokens or 0))
    if not (hit or miss):
        miss = max(0, int(in_tokens or 0))
    usd = (
        hit * pricing.input_cache_hit_per_million
        + miss * pricing.input_cache_miss_per_million
        + max(0, int(out_tokens or 0)) * pricing.output_per_million
    ) / 1_000_000
    return round(usd, 9)
