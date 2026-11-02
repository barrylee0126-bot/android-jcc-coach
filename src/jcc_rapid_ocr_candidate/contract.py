"""Independent contract; a crop extent is never a detected text box."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import math
from numbers import Real
import re
import struct
import zlib

SCHEMA = "jcc-rapid-ocr-candidate-v1"
MODEL_SHA256 = "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884"
MODEL_BYTES = 21_234_383
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_BYTES = 6 * 1024 * 1024
MAX_PIXELS = 2_000_000
MAX_TEXT_CHARS = 8192


class CandidateError(RuntimeError):
    """A fixed reason code, with no paths, OCR text or exception details."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FixedROI:
    """Caller-defined identity and exact dimensions of an already cropped PNG.

    This describes an input contract, not a calibrated region, text geometry,
    full-frame origin, freshness, field identity or visibility evidence.
    """

    roi_id: str
    width: int
    height: int

    def __post_init__(self):
        if not isinstance(self.roi_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", self.roi_id):
            raise CandidateError("invalid_roi_contract")
        if any(type(n) is not int or not 1 <= n <= 4096 for n in (self.width, self.height)):
            raise CandidateError("invalid_roi_contract")
        if self.width * self.height > MAX_PIXELS:
            raise CandidateError("invalid_roi_contract")

    def to_dict(self):
        return {"roi_id": self.roi_id, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class CropRequest:
    roi: FixedROI
    png: bytes

    def __post_init__(self):
        if not isinstance(self.roi, FixedROI):
            raise CandidateError("invalid_roi_contract")
        if type(self.png) is not bytes or not 0 < len(self.png) <= MAX_IMAGE_BYTES:
            raise CandidateError("invalid_image_bytes")

    @property
    def sha256(self):
        return hashlib.sha256(self.png).hexdigest()


@dataclass(frozen=True)
class Transcription:
    status: str
    reason: str
    roi: FixedROI
    input_sha256: str
    transcription: str | None = None
    raw_transcription: str | None = None
    raw_score: float | None = None
    request_wall_ms: float | None = None
    inference_ms: float | None = None

    def to_dict(self):
        # Authorization/evidence fields are constants, never caller parameters.
        return {
            "schema_version": SCHEMA,
            "status": self.status, "reason": self.reason,
            "evidence_kind": "transcription_only",
            "geometry_evidence": False, "text_boxes": None,
            "human_annotation": False, "live_use_authorized": False,
            "eligible_for_geometry_gate": False,
            "eligible_for_policy": False, "eligible_for_execution": False,
            "accuracy": None, "roi_contract": self.roi.to_dict(),
            "input_sha256": self.input_sha256,
            "transcription": self.transcription,
            "raw_transcription": self.raw_transcription,
            "raw_score": self.raw_score,
            "backend": {"library": "rapidocr", "version": "3.10.0",
                        "runtime": "onnxruntime", "runtime_version": "1.23.2",
                        "model_sha256": MODEL_SHA256,
                        "provider": "CPUExecutionProvider"},
            "timing_ms": {"request_wall": self.request_wall_ms,
                          "inference": self.inference_ms},
        }


def failure(request: CropRequest, reason: str, *, status="unknown"):
    return Transcription(status, reason, request.roi, request.sha256)


def normalize_raw(request: CropRequest, raw: dict, minimum_raw_score: float):
    """Validate, never concatenate, correct digits, select a line or parse values."""
    if (isinstance(minimum_raw_score, bool) or not isinstance(minimum_raw_score, Real)
            or not math.isfinite(minimum_raw_score) or not 0 <= minimum_raw_score <= 1):
        raise CandidateError("invalid_runtime_config")
    invalid = failure(request, "invalid_backend_output")
    if not isinstance(raw, dict) or raw.get("result_type") != "TextRecOutput":
        return invalid
    if raw.get("boxes") is not None:
        return failure(request, "unexpected_geometry")
    texts, scores = raw.get("txts"), raw.get("scores")
    if not isinstance(texts, (list, tuple)) or not isinstance(scores, (list, tuple)):
        return invalid
    if not texts and not scores:
        return failure(request, "empty_transcription")
    if len(texts) != 1 or len(scores) != 1:
        return failure(request, "ambiguous_transcription")
    text, score = texts[0], scores[0]
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        return invalid
    if isinstance(score, bool) or not isinstance(score, Real) or not math.isfinite(score) or not 0 <= score <= 1:
        return invalid
    elapsed = raw.get("inference_ms")
    if isinstance(elapsed, bool) or not isinstance(elapsed, Real) or not math.isfinite(elapsed) or elapsed < 0:
        return invalid
    result = Transcription("unknown", "empty_transcription", request.roi,
                           request.sha256, raw_transcription=text,
                           raw_score=float(score), inference_ms=float(elapsed))
    if not text.strip():
        return result
    if score < minimum_raw_score:
        return replace(result, reason="low_raw_score")
    # Preserve the exact machine string, including whitespace and Unicode.
    return replace(result, status="development_candidate", reason="transcription_available",
                   transcription=text)


def reconcile(observations):
    """Same-crop comparison only; agreement never changes the evidence class."""
    try:
        observations = tuple(observations)
    except TypeError:
        raise CandidateError("comparison_requires_two_transcriptions") from None
    if len(observations) < 2 or any(not isinstance(o, Transcription) for o in observations):
        raise CandidateError("comparison_requires_two_transcriptions")
    first = observations[0]
    base = replace(first, status="unknown", transcription=None, raw_transcription=None,
                   raw_score=None, request_wall_ms=None, inference_ms=None)
    if any(o.roi != first.roi or o.input_sha256 != first.input_sha256 for o in observations):
        return replace(base, reason="comparison_input_mismatch")
    if any(o.status != "development_candidate" or not o.transcription for o in observations):
        return replace(base, reason="comparison_incomplete")
    if any(o.transcription != first.transcription for o in observations):
        return replace(base, reason="transcription_conflict")
    return replace(base, status="development_candidate", reason="same_crop_agreement",
                   transcription=first.transcription)


def blank_png(width=96, height=48):
    """A synthetic white RGB PNG made using only the standard library."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    rows = (b"\x00" + b"\xff\xff\xff" * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))
