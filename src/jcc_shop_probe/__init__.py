"""Offline shop development candidates; never a game-state or action API."""

from .probe import (
    FieldCandidate, LexiconEntry, Rect, ShopLayout, ShopProbeResult, SlotLayout,
    SlotResult, probe_shop,
)

__all__ = [
    "FieldCandidate", "LexiconEntry", "Rect", "ShopLayout", "ShopProbeResult",
    "SlotLayout", "SlotResult", "probe_shop",
]
