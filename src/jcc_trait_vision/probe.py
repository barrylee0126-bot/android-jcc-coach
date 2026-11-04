"""Offline observations of one declared trait-rail row.

This module reads only pixels supplied by its caller.  A visible Chinese display
string is not a version-bound trait identity, and no result proves player,
phase, bench contents, legality, or a recommended action.  All successes are
unreviewed development candidates; unknown is the only result after a failed
binding, missing witness, opaque/animated row, or OCR disagreement.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import math
import re
import time
from typing import Protocol

from jcc_capacity_probe import FrameEvidence as CapacityEvidence
from jcc_capacity_probe import ProbeLayout, probe_capacity


METHOD = "trait-vision-v1"
IMPLEMENTATION_REVISION = "hash-bound-row-label-ratio-v1"
CANDIDATE = "development_candidate"
UNKNOWN = "unknown"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_RATIO = re.compile(r"(0|[1-9][0-9]{0,5})/([1-9][0-9]{0,5})(?:/([1-9][0-9]{0,5}))?\Z")
_MAX_IMAGE_BYTES = 25 * 1024 * 1024
_MAX_IMAGE_PIXELS = 16_000_000


class OCR(Protocol):
    def recognize(self, image_bytes: bytes) -> list[dict]: ...


def _ints(value, length: int) -> bool:
    return isinstance(value, (list, tuple)) and len(value) == length and all(type(x) is int for x in value)


def _roi_inside(roi: tuple[int, int, int, int], image_size: tuple[int, int]) -> bool:
    left, top, right, bottom = roi
    width, height = image_size
    return 0 <= left < right <= width and 0 <= top < bottom <= height


def _valid_score(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1


def parse_trait_ratio(text: str) -> tuple[int, tuple[int, ...]] | None:
    """Accept a displayed `n/c` or `n/c/c` string without OCR repair.

    The left value must not exceed any strictly increasing displayed threshold.
    This does not interpret the active threshold or map a text label to a game
    catalog identity.
    """
    if not isinstance(text, str):
        return None
    match = _RATIO.fullmatch(text.strip(" \t\r\n"))
    if not match:
        return None
    values = tuple(map(int, (match.group(1), match.group(2)) + ((match.group(3),) if match.group(3) else ())))
    observed, thresholds = values[0], values[1:]
    if observed > thresholds[0] or any(a >= b for a, b in zip(thresholds, thresholds[1:])):
        return None
    return observed, thresholds


@dataclass(frozen=True)
class FrameRecord:
    """Caller-declared replay identity, hash-bound to the supplied PNG/JPEG.

    `subject` and `scene` are retained as unverified declarations for downstream
    diagnostics. They are never used to create an action or upgrade OCR output.
    """

    case_id: str
    frame_id: str
    decoded_pts_s: float
    source_video_sha256: str
    subject: str = "unknown"
    scene: str = "unknown"
    validity: str = "single_frame_only"

    def __post_init__(self):
        if (not isinstance(self.case_id, str) or not _ID.fullmatch(self.case_id)
                or not isinstance(self.frame_id, str) or not _ID.fullmatch(self.frame_id)
                or not _valid_seconds(self.decoded_pts_s)
                or not isinstance(self.source_video_sha256, str) or not _SHA256.fullmatch(self.source_video_sha256)
                or self.subject not in ("unknown", "self_candidate", "other_candidate")
                or self.scene not in ("unknown", "preparation_candidate", "combat_candidate")
                or self.validity != "single_frame_only"):
            raise ValueError("invalid_frame_record")


def _valid_seconds(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


@dataclass(frozen=True)
class FieldEvidence:
    """Per-field visual witness; strings are unverified human/agent assertions."""

    encoded_sha256: str
    field_id: str
    roi: tuple[int, int, int, int]
    image_size: tuple[int, int]
    source_kind: str = "replay"
    layout_matches: bool | None = None
    visibility: str = "unknown"
    animation: str = "unknown"
    assessment_method: str = "agent_development_observation"

    def __post_init__(self):
        if (not isinstance(self.encoded_sha256, str) or not _SHA256.fullmatch(self.encoded_sha256)
                or not isinstance(self.field_id, str) or not _ID.fullmatch(self.field_id)
                or not _ints(self.roi, 4) or not _ints(self.image_size, 2)
                or any(x <= 0 for x in self.image_size) or not _roi_inside(tuple(self.roi), tuple(self.image_size))
                or self.source_kind not in ("synthetic", "replay")
                or self.layout_matches is not None and type(self.layout_matches) is not bool
                or self.visibility not in ("unknown", "occluded", "clear_candidate")
                or self.animation not in ("unknown", "observed", "not_observed_candidate")
                or self.assessment_method not in ("synthetic", "agent_development_observation")):
            raise ValueError("invalid_field_evidence")
        object.__setattr__(self, "roi", tuple(self.roi))
        object.__setattr__(self, "image_size", tuple(self.image_size))


@dataclass(frozen=True)
class TraitRowLayout:
    """Exact, private coordinates for a single display row; no defaults."""

    row_id: str
    image_size: tuple[int, int]
    row_roi: tuple[int, int, int, int]
    label_roi: tuple[int, int, int, int]
    ratio_roi: tuple[int, int, int, int]
    source_kind: str = "replay"
    scales: tuple[int, ...] = (1, 2)
    minimum_raw_score: float = 0.5
    ocr_scope: str = "crop"

    def __post_init__(self):
        if (not isinstance(self.row_id, str) or not _ID.fullmatch(self.row_id)
                or not _ints(self.image_size, 2) or any(x <= 0 for x in self.image_size)
                or any(not _ints(roi, 4) for roi in (self.row_roi, self.label_roi, self.ratio_roi))
                or any(not _roi_inside(tuple(roi), tuple(self.image_size))
                       for roi in (self.row_roi, self.label_roi, self.ratio_roi))
                or self.source_kind not in ("synthetic", "replay")
                or self.ocr_scope not in ("crop", "full_frame")
                or not isinstance(self.scales, (list, tuple)) or len(self.scales) < 2
                or len(set(self.scales)) != len(self.scales)
                or any(type(x) is not int or not 1 <= x <= 4 for x in self.scales)
                or not _valid_score(self.minimum_raw_score)):
            raise ValueError("invalid_trait_row_layout")
        row, label, ratio = tuple(self.row_roi), tuple(self.label_roi), tuple(self.ratio_roi)
        # The two declared regions may overlap slightly to retain a pixel margin
        # around vertically stacked text.  Binding still requires one distinct,
        # strictly interior OCR box for each region at every scale.
        if not (_nested(label, row) and _nested(ratio, row)):
            raise ValueError("row_subregions_invalid")
        object.__setattr__(self, "image_size", tuple(self.image_size))
        object.__setattr__(self, "row_roi", row)
        object.__setattr__(self, "label_roi", label)
        object.__setattr__(self, "ratio_roi", ratio)
        object.__setattr__(self, "scales", tuple(self.scales))


def _nested(inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> bool:
    return outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


@dataclass(frozen=True)
class TraitRowInput:
    layout: TraitRowLayout
    row_evidence: FieldEvidence
    label_evidence: FieldEvidence
    ratio_evidence: FieldEvidence

    def __post_init__(self):
        if not all(isinstance(x, FieldEvidence) for x in (self.row_evidence, self.label_evidence, self.ratio_evidence)):
            raise ValueError("invalid_trait_row_evidence")


@dataclass(frozen=True)
class PopulationInput:
    """A capacity probe plus a separate field witness for the same source frame."""

    layout: ProbeLayout
    evidence: CapacityEvidence
    field_evidence: FieldEvidence

    def __post_init__(self):
        if not isinstance(self.layout, ProbeLayout) or not isinstance(self.evidence, CapacityEvidence) or not isinstance(self.field_evidence, FieldEvidence):
            raise ValueError("invalid_population_input")


@dataclass(frozen=True)
class _Read:
    value: object | None = None
    score: float | None = None
    box: tuple[float, float, float, float] | None = None
    reason: str | None = None


def _evidence_blockers(evidence: FieldEvidence, *, expected_field_id: str, expected_roi, sha: str, image_size, source_kind: str) -> tuple[str, ...]:
    if evidence.encoded_sha256 != sha or evidence.field_id != expected_field_id or evidence.image_size != image_size or evidence.source_kind != source_kind:
        return ("field_evidence_mismatch",)
    if evidence.roi != expected_roi:
        return ("field_evidence_roi_mismatch",)
    if evidence.layout_matches is not True:
        return ("layout_mismatch" if evidence.layout_matches is False else "layout_identity_unknown",)
    if evidence.visibility != "clear_candidate":
        return ("occluded" if evidence.visibility == "occluded" else "visibility_unknown",)
    if evidence.animation != "not_observed_candidate":
        return ("animation_observed" if evidence.animation == "observed" else "animation_unknown",)
    if ((source_kind == "synthetic" and evidence.assessment_method != "synthetic")
            or (source_kind == "replay" and evidence.assessment_method != "agent_development_observation")):
        return ("evidence_authority_mismatch",)
    return ()


def _read_field(image, *, roi, scales, minimum_score, ocr: OCR, parser, scope: str) -> tuple[_Read, ...]:
    """OCR a crop, or OCR the source and retain exactly one interior ROI box."""
    from PIL import Image
    reads: list[_Read] = []
    source = image.crop(roi) if scope == "crop" else image.copy()
    offset = (roi[0], roi[1]) if scope == "crop" else (0, 0)
    with source as crop:
        for scale in scales:
            try:
                with crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.NEAREST) as scaled:
                    with io.BytesIO() as encoded:
                        scaled.save(encoded, format="PNG")
                        items = ocr.recognize(encoded.getvalue())
            except Exception:
                reads.append(_Read(reason="ocr_failed")); continue
            if not isinstance(items, list):
                reads.append(_Read(reason="invalid_ocr_response")); continue
            if scope == "full_frame":
                left, top, right, bottom = (value * scale for value in roi)
                selected, crossing = [], False
                for candidate in items:
                    if not isinstance(candidate, dict):
                        continue
                    try:
                        x, y, width, height = (candidate[key] for key in ("x", "y", "width", "height"))
                    except KeyError:
                        continue
                    if not all(_valid_number(value) for value in (x, y, width, height)):
                        continue
                    if left < x and top < y and x + width < right and y + height < bottom:
                        selected.append(candidate)
                    elif max(left, x) < min(right, x + width) and max(top, y) < min(bottom, y + height):
                        crossing = True
                if crossing:
                    reads.append(_Read(reason="text_touches_roi_edge")); continue
                items = selected
            if len(items) != 1 or not isinstance(items[0], dict):
                reads.append(_Read(reason="no_single_text_box")); continue
            item = items[0]
            try:
                text, x, y, width, height, score = (item[key] for key in ("text", "x", "y", "width", "height", "confidence"))
            except KeyError:
                reads.append(_Read(reason="invalid_ocr_response")); continue
            if (not isinstance(text, str) or any(not _valid_number(v) for v in (x, y, width, height, score)) or not _valid_score(score)):
                reads.append(_Read(reason="invalid_ocr_response")); continue
            crop_w, crop_h = crop.width * scale, crop.height * scale
            if not (0 <= x < x + width <= crop_w and 0 <= y < y + height <= crop_h):
                reads.append(_Read(score=score, reason="invalid_ocr_box")); continue
            if scope == "crop" and (x <= 0 or y <= 0 or x + width >= crop_w or y + height >= crop_h):
                reads.append(_Read(score=score, reason="text_touches_roi_edge")); continue
            if score < minimum_score:
                reads.append(_Read(score=score, reason="low_raw_score")); continue
            value = parser(text)
            if value is None:
                reads.append(_Read(score=score, reason="invalid_display_text")); continue
            reads.append(_Read(value=value, score=score,
                box=(offset[0] + x / scale, offset[1] + y / scale,
                     offset[0] + (x + width) / scale, offset[1] + (y + height) / scale)))
    return tuple(reads)


def _valid_number(value: object) -> bool:
    try:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _candidate_value(reads: tuple[_Read, ...]) -> tuple[object | None, tuple[str, ...], tuple[dict, ...]]:
    diagnostic = tuple({"status": CANDIDATE if read.value is not None else UNKNOWN,
                        "reason": read.reason, "raw_ocr_score": read.score,
                        "text_roi": list(read.box) if read.box else None}
                       for read in reads)
    if any(read.value is None for read in reads):
        return None, tuple(dict.fromkeys(("variant_incomplete", *(read.reason for read in reads if read.reason)))), diagnostic
    if any(read.value != reads[0].value for read in reads[1:]):
        return None, ("variant_disagreement",), diagnostic
    return reads[0].value, (), diagnostic


def _label_parser(text: str) -> str | None:
    normalized = "".join(text.split())
    return normalized if 1 <= len(normalized) <= 32 and not any(ord(char) < 32 for char in normalized) else None


def _frame_context(record: FrameRecord) -> dict:
    return {"case_id": record.case_id, "frame_id": record.frame_id, "decoded_pts_s": record.decoded_pts_s,
            "source_video_sha256": record.source_video_sha256, "subject": record.subject,
            "scene": record.scene, "validity": record.validity,
            "context_authority": "caller_declared_unverified"}


def _unknown_row(row: TraitRowInput, sha: str, record: FrameRecord, reasons: tuple[str, ...],
                 label_variants=(), ratio_variants=(), label_value=None, ratio_value=None) -> dict:
    return {"row_id": row.layout.row_id, "status": UNKNOWN,
            "label_status": CANDIDATE if label_value is not None else UNKNOWN,
            "ratio_status": CANDIDATE if ratio_value is not None else UNKNOWN,
            "display_label": label_value, "observed_count": ratio_value[0] if ratio_value else None,
            "displayed_thresholds": list(ratio_value[1]) if ratio_value else None, "trait_identity": "unbound",
            "reason_codes": list(reasons), "row_roi": list(row.layout.row_roi),
            "label_roi": list(row.layout.label_roi), "ratio_roi": list(row.layout.ratio_roi),
            "source_frame": {**_frame_context(record), "encoded_sha256": sha},
            "label_variants": list(label_variants), "ratio_variants": list(ratio_variants),
            "accuracy": None, "automatic_action": None}


def _probe_row(image, *, sha: str, record: FrameRecord, row: TraitRowInput, source_kind: str, ocr: OCR) -> dict:
    layout = row.layout
    if layout.source_kind != source_kind:
        return _unknown_row(row, sha, record, ("layout_mismatch",))
    blockers: list[str] = []
    for evidence, field_id, roi in ((row.row_evidence, layout.row_id, layout.row_roi),
                                    (row.label_evidence, layout.row_id + "-label", layout.label_roi),
                                    (row.ratio_evidence, layout.row_id + "-ratio", layout.ratio_roi)):
        blockers.extend(_evidence_blockers(evidence, expected_field_id=field_id, expected_roi=roi,
                                           sha=sha, image_size=layout.image_size, source_kind=source_kind))
    if blockers:
        return _unknown_row(row, sha, record, tuple(dict.fromkeys(blockers)))
    label, label_reasons, label_variants = _candidate_value(_read_field(
        image, roi=layout.label_roi, scales=layout.scales, minimum_score=layout.minimum_raw_score,
        ocr=ocr, parser=_label_parser, scope=layout.ocr_scope))
    ratio, ratio_reasons, ratio_variants = _candidate_value(_read_field(
        image, roi=layout.ratio_roi, scales=layout.scales, minimum_score=layout.minimum_raw_score,
        ocr=ocr, parser=parse_trait_ratio, scope=layout.ocr_scope))
    if label is None or ratio is None:
        return _unknown_row(row, sha, record, tuple(dict.fromkeys((*label_reasons, *ratio_reasons))),
                            label_variants, ratio_variants, label, ratio)
    observed, thresholds = ratio
    return {"row_id": layout.row_id, "status": CANDIDATE, "label_status": CANDIDATE,
            "ratio_status": CANDIDATE, "display_label": label,
            "observed_count": observed, "displayed_thresholds": list(thresholds),
            "trait_identity": "unbound", "reason_codes": [], "row_roi": list(layout.row_roi),
            "label_roi": list(layout.label_roi), "ratio_roi": list(layout.ratio_roi),
            "source_frame": {**_frame_context(record), "encoded_sha256": sha},
            "label_variants": list(label_variants), "ratio_variants": list(ratio_variants),
            "accuracy": None, "automatic_action": None}


def probe_trait_frame(image_bytes: bytes, *, source_kind: str, frame: FrameRecord,
                      population: PopulationInput | None, trait_rows: tuple[TraitRowInput, ...],
                      ocr: OCR | None, expected_sha256: str | None = None) -> dict:
    """Return hash-bound OCR candidates for one supplied replay frame.

    There is no device access, persistence, version-catalog lookup, timing
    freshness check, strategy output, or action in this function.
    """
    from PIL import Image, UnidentifiedImageError
    if source_kind not in ("synthetic", "replay") or not isinstance(frame, FrameRecord):
        raise ValueError("offline_source_and_frame_required")
    if not isinstance(image_bytes, bytes) or not isinstance(trait_rows, tuple) or not all(isinstance(x, TraitRowInput) for x in trait_rows):
        raise ValueError("invalid_trait_probe_input")
    if ocr is not None and not hasattr(ocr, "recognize"):
        raise ValueError("invalid_ocr")
    if expected_sha256 is not None and (not isinstance(expected_sha256, str) or not _SHA256.fullmatch(expected_sha256)):
        raise ValueError("invalid_expected_sha256")
    started = time.perf_counter()
    sha = hashlib.sha256(image_bytes).hexdigest()
    base = {"schema_version": METHOD, "implementation_revision": IMPLEMENTATION_REVISION,
            "source_frame": {**_frame_context(frame), "encoded_sha256": sha},
            "population": None, "trait_rows": [], "accuracy": None, "automatic_action": None}
    if expected_sha256 is not None and sha != expected_sha256:
        base.update(status=UNKNOWN, reason_codes=["source_hash_mismatch"], processing_seconds=time.perf_counter() - started)
        return base
    if not 0 < len(image_bytes) <= _MAX_IMAGE_BYTES:
        base.update(status=UNKNOWN, reason_codes=["image_unreadable_or_too_large"], processing_seconds=time.perf_counter() - started)
        return base
    try:
        with Image.open(io.BytesIO(image_bytes)) as original:
            if original.format not in ("PNG", "JPEG") or getattr(original, "n_frames", 1) != 1 or original.getexif().get(274, 1) != 1:
                raise ValueError("unsupported")
            if math.prod(original.size) > _MAX_IMAGE_PIXELS:
                raise ValueError("too_large")
            actual_size = original.size
            original.load()
            image = original.convert("RGB")
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        base.update(status=UNKNOWN, reason_codes=["image_unreadable"], processing_seconds=time.perf_counter() - started)
        return base
    with image:
        if ocr is None:
            base.update(status=UNKNOWN, reason_codes=["ocr_unavailable"], processing_seconds=time.perf_counter() - started)
            return base
        rows = []
        for row in trait_rows:
            if row.layout.image_size != actual_size:
                rows.append(_unknown_row(row, sha, frame, ("image_geometry_mismatch",)))
            else:
                rows.append(_probe_row(image, sha=sha, record=frame, row=row, source_kind=source_kind, ocr=ocr))
        if population is not None:
            pop = _probe_population(image_bytes, sha=sha, frame=frame, source_kind=source_kind,
                                    actual_size=actual_size, population=population, ocr=ocr)
        else:
            pop = {"status": UNKNOWN, "reason_codes": ["population_unconfigured"], "source_frame": {**_frame_context(frame), "encoded_sha256": sha}}
        base.update(population=pop, trait_rows=rows,
                    status=CANDIDATE if pop.get("status") == CANDIDATE or any(row["status"] == CANDIDATE for row in rows) else UNKNOWN,
                    reason_codes=[], processing_seconds=time.perf_counter() - started)
        return base


def _probe_population(image_bytes, *, sha, frame, source_kind, actual_size, population, ocr):
    layout, evidence, witness = population.layout, population.evidence, population.field_evidence
    reasons = _evidence_blockers(witness, expected_field_id=layout.layout_id, expected_roi=layout.roi,
                                 sha=sha, image_size=actual_size, source_kind=source_kind)
    if reasons or layout.image_size != actual_size:
        return {"status": UNKNOWN, "displayed_deployed": None, "capacity": None, "empty_slots": None,
                "reason_codes": list(reasons or ("image_geometry_mismatch",)), "input_roi": list(layout.roi),
                "source_frame": {**_frame_context(frame), "encoded_sha256": sha}, "automatic_action": None}
    result = probe_capacity(image_bytes, source_kind=source_kind, layout=layout, evidence=evidence,
                            ocr=ocr, expected_sha256=sha).to_dict()
    return {"status": result["status"], "displayed_deployed": result["displayed_deployed"],
            "capacity": result["capacity"], "empty_slots": result["empty_slots"],
            "reason_codes": result["reason_codes"], "input_roi": result["input_roi"],
            "source_frame": {**_frame_context(frame), "encoded_sha256": sha},
            "variants": result["confidence"]["variants"], "automatic_action": None}
