"""Offline, fail-closed display-label and trait-ratio observations."""

from .probe import (
    CANDIDATE,
    FieldEvidence,
    FrameRecord,
    PopulationInput,
    TraitRowInput,
    TraitRowLayout,
    parse_trait_ratio,
    probe_trait_frame,
)
from .gray_dual_ratio import GrayDualRatioInput, GrayDualRatioLayout, probe_gray_dual_ratio

__all__ = [
    "CANDIDATE", "FieldEvidence", "FrameRecord", "PopulationInput",
    "TraitRowInput", "TraitRowLayout", "parse_trait_ratio", "probe_trait_frame",
    "GrayDualRatioInput", "GrayDualRatioLayout", "probe_gray_dual_ratio",
]
