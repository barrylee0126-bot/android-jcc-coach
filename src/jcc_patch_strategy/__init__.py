"""Offline comparisons for the explicitly announced parts of a patch.

This package is intentionally narrower than a game strategy engine.  It only
applies numeric changes that the caller supplies in a version-change ledger to
explicitly synthetic scenarios.  It never treats the ledger as a complete
catalogue, a device binding, or permission to advise an action.
"""

from .core import REQUEST_FORMAT, RESULT_FORMAT, analyze, analyze_json

__all__ = ["REQUEST_FORMAT", "RESULT_FORMAT", "analyze", "analyze_json"]
