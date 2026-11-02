"""Optional, offline, transcription-only experiment. No device or policy imports."""

from .contract import CandidateError, CropRequest, FixedROI, Transcription, reconcile
from .runtime import RapidOCRCandidate, RuntimeConfig

__all__ = [
    "CandidateError", "CropRequest", "FixedROI", "Transcription", "reconcile",
    "RapidOCRCandidate", "RuntimeConfig",
]
