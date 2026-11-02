"""Independent conditional economy lab; imports perform no I/O."""

from .assumptions import source_catalog, ten_gold_five_cap_hypothesis
from .core import (
    GoldCandidate, HypotheticalPrice, InterestAssumption, MAX_EXACT_INTEGER,
    analyze_purchases, analyze_request,
)

__all__ = [
    "GoldCandidate", "HypotheticalPrice", "InterestAssumption", "MAX_EXACT_INTEGER",
    "analyze_purchases", "analyze_request", "source_catalog", "ten_gold_five_cap_hypothesis",
]
