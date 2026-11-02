"""Offline detector-geometry normalization for shadow OCR experiments.

This module intentionally has no device, game-state, policy or execution
imports.  A detector box only describes the supplied crop coordinates.
"""

from .contract import GeometryCandidate, GeometryError, normalize_full_result

__all__ = ["GeometryCandidate", "GeometryError", "normalize_full_result"]
