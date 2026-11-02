"""Small, injected command gate for one verified Android input at a time.

This package does not open a device, capture frames, or make game decisions.
It turns a caller-reviewed fresh frame plus a reviewed coordinate calibration
into one ADB command, then leaves the caller responsible for a fresh readback.
"""

from .operator import (
    DEFAULT_ADB_WRAPPER,
    Action,
    ActionOutcome,
    CoordinateCalibration,
    FastOperator,
    FrameEvidence,
    GateDecision,
)

__all__ = [
    "DEFAULT_ADB_WRAPPER",
    "Action",
    "ActionOutcome",
    "CoordinateCalibration",
    "FastOperator",
    "FrameEvidence",
    "GateDecision",
]
