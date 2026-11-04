"""Explicit-input lineup candidates for a human-operated coach.

This package never reads a game client or chooses a click.  It only compares
fully declared lineups against fully declared trait thresholds.
"""

from .core import plan, plan_json, plan_partial_trait_hint, plan_partial_trait_hint_json

__all__ = ["plan", "plan_json", "plan_partial_trait_hint", "plan_partial_trait_hint_json"]
