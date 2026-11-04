"""A versioned, related-view candidate for one displayed trait ratio.

This is an offline development candidate only.  The two views use the same
frame and OCR backend, so their agreement is not independent review, truth, or
an accuracy estimate.  It never binds a trait identity, player, phase, or move.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import math
import time

from .probe import (CANDIDATE, UNKNOWN, FieldEvidence, FrameRecord, OCR, _ID,
                    _MAX_IMAGE_BYTES, _MAX_IMAGE_PIXELS, _SHA256, _ints,
                    _roi_inside, _valid_number, _valid_score, parse_trait_ratio)


METHOD = "trait-ratio-gray-dual-roi-v1"
IMPLEMENTATION_REVISION = "nearest2x-gray-autocontrast-two-related-views-v1"


@dataclass(frozen=True)
class GrayDualRatioLayout:
    """Two fixed, vertically offset ROIs for a single displayed ratio field."""

    layout_id: str
    image_size: tuple[int, int]
    up_1px_roi: tuple[int, int, int, int]
    down_1px_roi: tuple[int, int, int, int]
    source_kind: str = "replay"
    scale: int = 2
    minimum_raw_score: float = 0.3
    minimum_inverse_iou: float = 0.98

    def __post_init__(self):
        if (not isinstance(self.layout_id, str) or not _ID.fullmatch(self.layout_id)
                or not _ints(self.image_size, 2) or any(value <= 0 for value in self.image_size)
                or any(not _ints(roi, 4) or not _roi_inside(tuple(roi), tuple(self.image_size))
                       for roi in (self.up_1px_roi, self.down_1px_roi))
                or self.source_kind not in ("synthetic", "replay")
                or type(self.scale) is not int or self.scale != 2
                or not _valid_score(self.minimum_raw_score)
                or not _valid_iou(self.minimum_inverse_iou)):
            raise ValueError("invalid_gray_dual_ratio_layout")
        up, down = tuple(self.up_1px_roi), tuple(self.down_1px_roi)
        # Both are predeclared one-pixel offsets around one logical ratio line.
        if not (up[0] == down[0] and up[2] == down[2]
                and up[3] - up[1] == down[3] - down[1]
                and down[1] == up[1] + 2 and down[3] == up[3] + 2):
            raise ValueError("dual_roi_offset_contract_invalid")
        object.__setattr__(self, "image_size", tuple(self.image_size))
        object.__setattr__(self, "up_1px_roi", up)
        object.__setattr__(self, "down_1px_roi", down)


def _valid_iou(value: object) -> bool:
    try:
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value) and 0.9 <= value <= 1)
    except (OverflowError, TypeError, ValueError):
        return False


@dataclass(frozen=True)
class GrayDualRatioInput:
    layout: GrayDualRatioLayout
    up_1px_evidence: FieldEvidence
    down_1px_evidence: FieldEvidence

    def __post_init__(self):
        if not isinstance(self.layout, GrayDualRatioLayout) or not isinstance(self.up_1px_evidence, FieldEvidence) or not isinstance(self.down_1px_evidence, FieldEvidence):
            raise ValueError("invalid_gray_dual_ratio_input")


@dataclass(frozen=True)
class _View:
    value: tuple[int, tuple[int, ...]] | None = None
    score: float | None = None
    source_box: tuple[float, float, float, float] | None = None
    reason: str | None = None


def _frame_context(frame: FrameRecord, sha: str) -> dict:
    return {"case_id": frame.case_id, "frame_id": frame.frame_id,
            "decoded_pts_s": frame.decoded_pts_s, "source_video_sha256": frame.source_video_sha256,
            "encoded_sha256": sha, "subject": frame.subject, "scene": frame.scene,
            "validity": frame.validity, "context_authority": "caller_declared_unverified"}


def _blockers(evidence: FieldEvidence, *, field_id: str, roi, sha: str, image_size, source_kind: str) -> tuple[str, ...]:
    if evidence.encoded_sha256 != sha or evidence.field_id != field_id or evidence.roi != roi:
        return ("field_evidence_mismatch",)
    if evidence.image_size != image_size or evidence.source_kind != source_kind:
        return ("field_evidence_mismatch",)
    if evidence.layout_matches is not True:
        return ("layout_mismatch" if evidence.layout_matches is False else "layout_identity_unknown",)
    if evidence.visibility != "clear_candidate":
        return ("occluded" if evidence.visibility == "occluded" else "visibility_unknown",)
    if evidence.animation != "not_observed_candidate":
        return ("animation_observed" if evidence.animation == "observed" else "animation_unknown",)
    expected_method = "synthetic" if source_kind == "synthetic" else "agent_development_observation"
    return () if evidence.assessment_method == expected_method else ("evidence_authority_mismatch",)


def _one_view(image, *, roi, layout: GrayDualRatioLayout, ocr: OCR) -> _View:
    from PIL import Image, ImageOps
    try:
        with image.crop(roi) as crop:
            with crop.resize((crop.width * layout.scale, crop.height * layout.scale), Image.Resampling.NEAREST) as enlarged:
                with ImageOps.autocontrast(ImageOps.grayscale(enlarged)) as prepared:
                    with io.BytesIO() as encoded:
                        prepared.save(encoded, format="PNG")
                        items = ocr.recognize(encoded.getvalue())
    except Exception:
        return _View(reason="ocr_failed")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return _View(reason="no_single_text_box")
    item = items[0]
    try:
        text, x, y, width, height, score = (item[key] for key in ("text", "x", "y", "width", "height", "confidence"))
    except KeyError:
        return _View(reason="invalid_ocr_response")
    if (not isinstance(text, str) or any(not _valid_number(value) for value in (x, y, width, height, score))
            or not _valid_score(score)):
        return _View(reason="invalid_ocr_response")
    crop_width, crop_height = (roi[2] - roi[0]) * layout.scale, (roi[3] - roi[1]) * layout.scale
    if not (0 <= x < x + width <= crop_width and 0 <= y < y + height <= crop_height):
        return _View(score=score, reason="invalid_ocr_box")
    if x <= 0 or y <= 0 or x + width >= crop_width or y + height >= crop_height:
        return _View(score=score, reason="text_touches_roi_edge")
    if score < layout.minimum_raw_score:
        return _View(score=score, reason="low_raw_score")
    value = parse_trait_ratio(text)
    if value is None:
        return _View(score=score, reason="invalid_display_text")
    return _View(value=value, score=score,
                 source_box=(roi[0] + x / layout.scale, roi[1] + y / layout.scale,
                             roi[0] + (x + width) / layout.scale, roi[1] + (y + height) / layout.scale))


def _iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    left, top, right, bottom = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    overlap = max(0.0, right - left) * max(0.0, bottom - top)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - overlap
    return overlap / union if union > 0 else 0.0


def _view_summary(view: _View, view_id: str, roi) -> dict:
    return {"view_id": view_id, "source_roi": list(roi),
            "preprocessing": "nearest_2x_gray_autocontrast", "status": CANDIDATE if view.value else UNKNOWN,
            "reason": view.reason, "raw_ocr_score": view.score,
            "inverse_source_box": list(view.source_box) if view.source_box else None}


def _result(frame, sha, layout, up, down, *, reasons=(), iou=None, started) -> dict:
    candidate = not reasons and up.value is not None and down.value is not None
    value = up.value if candidate else None
    return {"schema_version": METHOD, "implementation_revision": IMPLEMENTATION_REVISION,
            "source_frame": _frame_context(frame, sha), "layout_id": layout.layout_id,
            "source_kind": layout.source_kind, "status": CANDIDATE if candidate else UNKNOWN,
            "evidence_status": "machine_candidate" if candidate else UNKNOWN,
            "observed_count": value[0] if value else None,
            "displayed_thresholds": list(value[1]) if value else None,
            "trait_identity": "unbound", "reason_codes": list(reasons),
            "preprocessing": {"version": "gray-dual-roi-v1", "scale": layout.scale,
                              "operation": "nearest_2x_gray_autocontrast",
                              "related_view_warning": "same_frame_same_backend_not_independent_review",
                              "minimum_raw_score": layout.minimum_raw_score,
                              "minimum_inverse_iou": layout.minimum_inverse_iou},
            "inverse_box_iou": iou, "views": [_view_summary(up, "up_1px", layout.up_1px_roi),
                                                   _view_summary(down, "down_1px", layout.down_1px_roi)],
            "accuracy": None, "automatic_action": None,
            "processing_seconds": time.perf_counter() - started}


def probe_gray_dual_ratio(image_bytes: bytes, *, source_kind: str, frame: FrameRecord,
                           candidate: GrayDualRatioInput, ocr: OCR | None,
                           expected_sha256: str | None = None) -> dict:
    """Read one ratio via two related predeclared views, otherwise abstain."""
    from PIL import Image, UnidentifiedImageError
    if source_kind not in ("synthetic", "replay") or not isinstance(frame, FrameRecord) or not isinstance(candidate, GrayDualRatioInput):
        raise ValueError("offline_source_frame_and_candidate_required")
    if ocr is not None and not hasattr(ocr, "recognize"):
        raise ValueError("invalid_ocr")
    if expected_sha256 is not None and (not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256)):
        raise ValueError("invalid_expected_sha256")
    if not isinstance(image_bytes, bytes):
        raise ValueError("image_bytes_required")
    started = time.perf_counter(); sha = hashlib.sha256(image_bytes).hexdigest(); layout = candidate.layout
    empty = _View(reason="not_run")
    if expected_sha256 is not None and sha != expected_sha256:
        return _result(frame, sha, layout, empty, empty, reasons=("source_hash_mismatch",), started=started)
    if not 0 < len(image_bytes) <= _MAX_IMAGE_BYTES:
        return _result(frame, sha, layout, empty, empty, reasons=("image_unreadable_or_too_large",), started=started)
    try:
        with Image.open(io.BytesIO(image_bytes)) as original:
            if original.format not in ("PNG", "JPEG") or getattr(original, "n_frames", 1) != 1 or original.getexif().get(274, 1) != 1:
                raise ValueError("unsupported")
            if math.prod(original.size) > _MAX_IMAGE_PIXELS:
                raise ValueError("large")
            actual_size = original.size; original.load(); image = original.convert("RGB")
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return _result(frame, sha, layout, empty, empty, reasons=("image_unreadable",), started=started)
    with image:
        if layout.source_kind != source_kind or layout.image_size != actual_size:
            return _result(frame, sha, layout, empty, empty, reasons=("layout_mismatch",), started=started)
        blockers = (*_blockers(candidate.up_1px_evidence, field_id=layout.layout_id + "-up-1px", roi=layout.up_1px_roi,
                               sha=sha, image_size=actual_size, source_kind=source_kind),
                    *_blockers(candidate.down_1px_evidence, field_id=layout.layout_id + "-down-1px", roi=layout.down_1px_roi,
                               sha=sha, image_size=actual_size, source_kind=source_kind))
        if blockers:
            return _result(frame, sha, layout, empty, empty, reasons=tuple(dict.fromkeys(blockers)), started=started)
        if ocr is None:
            return _result(frame, sha, layout, empty, empty, reasons=("ocr_unavailable",), started=started)
        up, down = _one_view(image, roi=layout.up_1px_roi, layout=layout, ocr=ocr), _one_view(image, roi=layout.down_1px_roi, layout=layout, ocr=ocr)
        if up.value is None or down.value is None:
            return _result(frame, sha, layout, up, down, reasons=tuple(dict.fromkeys(("view_incomplete", *(x.reason for x in (up, down) if x.reason)))), started=started)
        if up.value != down.value:
            return _result(frame, sha, layout, up, down, reasons=("view_disagreement",), started=started)
        overlap = _iou(up.source_box, down.source_box)
        if overlap < layout.minimum_inverse_iou:
            return _result(frame, sha, layout, up, down, reasons=("inverse_box_iou_below_threshold",), iou=overlap, started=started)
        return _result(frame, sha, layout, up, down, iou=overlap, started=started)
