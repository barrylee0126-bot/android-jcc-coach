"""Read one explicitly located n/c display, or abstain.

All successes are unreviewed development candidates. A displayed numerator is
not a count of detected pieces. Same-backend scale agreement is not independent
verification. Context declarations below are caller assertions, not truths
established by this module; actual game phase, player and usable bench remain
unknown in every output. Nothing here connects to a device or emits an action.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import math
import re
import time
from typing import Protocol


METHOD = "capacity-probe-v1"
IMPLEMENTATION_REVISION = "roi-bound-sanitized-variants-v2"
CANDIDATE = "development_candidate"
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
KINDS = ("synthetic", "replay")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
# Six digits is an input/resource limit, not a game-mode capacity limit.
_RATIO = re.compile(r"(0|[1-9][0-9]{0,5})/([1-9][0-9]{0,5})\Z")


class OCR(Protocol):
    def recognize(self, image_bytes: bytes) -> list[dict]: ...


def _integers(values, count: int) -> bool:
    return (isinstance(values, (tuple, list)) and len(values) == count
            and all(type(v) is int for v in values))


@dataclass(frozen=True)
class ProbeLayout:
    """Private, exact-size, caller-selected display ROI; no default coordinates."""

    layout_id: str
    image_size: tuple[int, int]
    roi: tuple[int, int, int, int]
    source_kind: str
    rotation_applied_degrees: int = 0
    scales: tuple[int, ...] = (1, 2)
    minimum_raw_score: float = 0.5

    def __post_init__(self):
        if (not isinstance(self.layout_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", self.layout_id)
                or self.source_kind not in KINDS
                or not _integers(self.image_size, 2)
                or any(v <= 0 for v in self.image_size)
                or math.prod(self.image_size) > MAX_IMAGE_PIXELS
                or not _integers(self.roi, 4)
                or type(self.rotation_applied_degrees) is not int
                or self.rotation_applied_degrees not in (0, 90, 180, 270)
                or not isinstance(self.scales, (tuple, list))):
            raise ValueError("invalid_layout")
        w, h = self.image_size
        l, t, r, b = self.roi
        if not (0 <= l < r <= w and 0 <= t < b <= h):
            raise ValueError("invalid_roi")
        if (len(self.scales) < 2 or len(set(self.scales)) != len(self.scales)
                or any(type(v) is not int or not 1 <= v <= 4 for v in self.scales)):
            raise ValueError("two_distinct_scales_required")
        score = self.minimum_raw_score
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score) or not 0 <= score <= 1):
            raise ValueError("invalid_minimum_raw_score")
        object.__setattr__(self, "image_size", tuple(self.image_size))
        object.__setattr__(self, "roi", tuple(self.roi))
        object.__setattr__(self, "scales", tuple(self.scales))


@dataclass(frozen=True)
class FrameEvidence:
    """Frame-and-exact-ROI-bound assessment, never a human label/review.

    clear_candidate/not_observed_candidate describe a single-frame visual
    assessment only. They cannot establish temporal stability. Unknown evidence
    fails closed. An observed animation or occlusion always prevents a result.
    """

    encoded_sha256: str
    layout_id: str
    source_kind: str
    image_size: tuple[int, int]
    rotation_applied_degrees: int
    roi: tuple[int, int, int, int]
    layout_matches: bool | None = None
    visibility: str = "unknown"
    animation: str = "unknown"
    assessment_method: str = "agent_development_observation"

    def __post_init__(self):
        if (not isinstance(self.encoded_sha256, str)
                or not _SHA256.fullmatch(self.encoded_sha256)
                or not isinstance(self.layout_id, str) or not self.layout_id
                or self.source_kind not in KINDS
                or not _integers(self.image_size, 2)
                or any(v <= 0 for v in self.image_size)
                or type(self.rotation_applied_degrees) is not int
                or self.rotation_applied_degrees not in (0, 90, 180, 270)
                or not _integers(self.roi, 4)
                or self.layout_matches is not None and type(self.layout_matches) is not bool
                or self.visibility not in ("unknown", "occluded", "clear_candidate")
                or self.animation not in ("unknown", "observed", "not_observed_candidate")
                or self.assessment_method not in ("synthetic", "agent_development_observation")):
            raise ValueError("invalid_frame_evidence")
        w, h = self.image_size
        l, t, r, b = self.roi
        if not (0 <= l < r <= w and 0 <= t < b <= h):
            raise ValueError("invalid_evidence_roi")
        object.__setattr__(self, "image_size", tuple(self.image_size))
        object.__setattr__(self, "roi", tuple(self.roi))


def parse_capacity_ratio(text: str) -> tuple[int, int] | None:
    """Strict finite n/c, c>0 and 0<=n<=c. No OCR repair or inferred limit."""
    if not isinstance(text, str):
        return None
    match = _RATIO.fullmatch(text.strip(" \t\r\n"))
    if not match:
        return None
    used, capacity = map(int, match.groups())
    return (used, capacity) if used <= capacity else None


@dataclass(frozen=True)
class _Variant:
    scale: int
    pair: tuple[int, int] | None = None
    reason: str | None = None
    raw_score: float | None = None
    text_roi: tuple[float, float, float, float] | None = None
    processing_seconds: float = 0.0

    def diagnostic(self, *, overall_candidate: bool) -> VariantDiagnostic:
        # Never retain the internal object in CapacityResult: hiding pair only
        # in JSON still leaves Python attributes/asdict/repr exposing it.
        reason = self.reason
        if not overall_candidate and reason is None:
            reason = "discarded_due_to_unknown_result"
        return VariantDiagnostic(
            self.scale, CANDIDATE if overall_candidate else "unknown",
            reason,
            self.raw_score, self.text_roi, self.processing_seconds,
        )


@dataclass(frozen=True)
class VariantDiagnostic:
    """Public, sanitized diagnostic; contains no per-variant numeric answer."""

    scale: int
    status: str
    reason: str | None
    raw_score: float | None
    text_roi: tuple[float, float, float, float] | None
    processing_seconds: float

    def summary(self) -> dict:
        return {"scale": self.scale, "status": self.status,
                "reason": self.reason, "raw_ocr_score": self.raw_score,
                "text_roi": list(self.text_roi) if self.text_roi else None,
                "processing_seconds": self.processing_seconds}


@dataclass(frozen=True)
class CapacityResult:
    encoded_sha256: str
    source_kind: str
    image_size: tuple[int, int] | None
    layout_id: str | None
    input_roi: tuple[int, int, int, int] | None
    rotation_applied_degrees: int | None
    status: str
    displayed_deployed: int | None
    capacity: int | None
    empty_slots: int | None
    reasons: tuple[str, ...]
    variants: tuple[VariantDiagnostic, ...]
    processing_seconds: float

    @property
    def hint(self) -> str:
        limits = "阶段、主体、备战席是否有可上场棋子均尚未证实。"
        if self.status != CANDIDATE:
            return "本帧上场人数/容量未知，无法判断空位。" + limits
        if self.empty_slots:
            return (f"本帧候选空位为{self.empty_slots}；若为己方备战且备战席有可用棋子，可检查空位。"
                    + limits)
        return "本帧候选空位为0。" + limits

    def to_dict(self) -> dict:
        return {
            "schema_version": METHOD,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "source_frame": {
                "anonymous_ref": "sha256:" + self.encoded_sha256,
                "encoded_sha256": self.encoded_sha256,
                "source_kind": self.source_kind,
                "image_size": list(self.image_size) if self.image_size else None,
                "rotation_applied_degrees": self.rotation_applied_degrees,
            },
            "layout_id": self.layout_id,
            "status": self.status,
            "displayed_deployed": self.displayed_deployed,
            "capacity": self.capacity,
            "empty_slots": self.empty_slots,
            "reason_codes": list(self.reasons),
            "confidence": {
                "calibrated_probability": None,
                "basis": "same_apple_vision_backend_scale_comparison",
                "variants": [v.summary() for v in self.variants],
            },
            "input_roi": list(self.input_roi) if self.input_roi else None,
            "context": {
                "phase": "unknown", "subject": "unknown", "deployable_bench_unit": "unknown",
                "temporal_stability": "unverified_single_frame",
                "review_status": "unreviewed_machine_agent_candidate",
            },
            "conditional_hint": self.hint,
            "automatic_action": None,
            "processing_seconds": self.processing_seconds,
            "accuracy": None,
        }


def _read_variant(items, layout: ProbeLayout, scale: int) -> _Variant:
    def unknown(reason, score=None):
        return _Variant(scale, reason=reason, raw_score=score)

    if not isinstance(items, list):
        return unknown("invalid_ocr_response")
    if not items:
        return unknown("no_text")
    if len(items) != 1:
        return unknown("multiple_text_boxes")
    item = items[0]
    if not isinstance(item, dict) or not isinstance(item.get("text"), str):
        return unknown("invalid_ocr_response")
    try:
        x, y, w, h, score = (item[k] for k in ("x", "y", "width", "height", "confidence"))
    except KeyError:
        return unknown("invalid_ocr_response")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in (x, y, w, h, score)) or not 0 <= score <= 1:
        return unknown("invalid_ocr_response")
    l, t, r, b = layout.roi
    crop_w, crop_h = (r - l) * scale, (b - t) * scale
    if not (0 <= x < x + w <= crop_w and 0 <= y < y + h <= crop_h):
        return unknown("invalid_ocr_box", score)
    if x <= 0 or y <= 0 or x + w >= crop_w or y + h >= crop_h:
        return unknown("text_touches_roi_edge", score)
    if score < layout.minimum_raw_score:
        return unknown("low_raw_score", score)
    pair = parse_capacity_ratio(item["text"])
    if pair is None:
        return unknown("incomplete_or_invalid_ratio", score)
    box = (l + x / scale, t + y / scale, l + (x + w) / scale, t + (y + h) / scale)
    return _Variant(scale, pair=pair, raw_score=score, text_roi=box)


def probe_capacity(image_bytes: bytes, *, source_kind: str,
                   layout: ProbeLayout | None = None, evidence: FrameEvidence | None = None,
                   ocr: OCR | None = None, expected_sha256: str | None = None) -> CapacityResult:
    """Offline one-frame probe; unknown is never converted to full or zero.

    The byte hash and exact ROI bind evidence to the supplied image and crop.
    A re-encoding has a different identity. There is no latest-frame/TTL check,
    phase/owner recognition, bench detection, label write, or automatic action.
    """
    from dataclasses import replace
    from PIL import Image, UnidentifiedImageError

    if source_kind not in KINDS:
        raise ValueError("offline_source_kind_required")
    if not isinstance(image_bytes, bytes):
        raise ValueError("image_bytes_required")
    if layout is not None and not isinstance(layout, ProbeLayout):
        raise ValueError("invalid_layout_type")
    if evidence is not None and not isinstance(evidence, FrameEvidence):
        raise ValueError("invalid_evidence_type")
    if expected_sha256 is not None and (not isinstance(expected_sha256, str)
                                         or not _SHA256.fullmatch(expected_sha256)):
        raise ValueError("invalid_expected_sha256")
    started = time.perf_counter()
    sha = hashlib.sha256(image_bytes).hexdigest()
    actual_size = None

    def finish(reasons=(), variants=(), pair=None):
        return CapacityResult(
            sha, source_kind, actual_size, layout.layout_id if layout else None,
            layout.roi if layout else None, layout.rotation_applied_degrees if layout else None,
            CANDIDATE if pair is not None else "unknown",
            pair[0] if pair else None, pair[1] if pair else None,
            pair[1] - pair[0] if pair else None, tuple(reasons),
            tuple(v.diagnostic(overall_candidate=pair is not None) for v in variants),
            time.perf_counter() - started,
        )

    if expected_sha256 is not None and sha != expected_sha256:
        return finish(("source_hash_mismatch",))
    if not 0 < len(image_bytes) <= MAX_IMAGE_BYTES:
        return finish(("image_unreadable_or_too_large",))
    if layout is None:
        return finish(("layout_unconfigured",))
    if layout.source_kind != source_kind:
        return finish(("layout_mismatch",))
    try:
        with Image.open(io.BytesIO(image_bytes)) as original:
            if original.format not in ("PNG", "JPEG") or getattr(original, "n_frames", 1) != 1:
                return finish(("unsupported_image",))
            actual_size = original.size
            if math.prod(original.size) > MAX_IMAGE_PIXELS:
                return finish(("image_too_large",))
            if original.getexif().get(274, 1) != 1:
                return finish(("unapplied_exif_rotation",))
            if original.size != layout.image_size:
                return finish(("image_geometry_mismatch",))
            original.load()
            image = original.convert("RGB")
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        return finish(("image_unreadable",))
    with image:
        if evidence is None:
            return finish(("frame_evidence_missing",))
        if (evidence.encoded_sha256 != sha or evidence.layout_id != layout.layout_id
                or evidence.source_kind != source_kind or evidence.image_size != actual_size
                or evidence.rotation_applied_degrees != layout.rotation_applied_degrees):
            return finish(("frame_evidence_mismatch",))
        if evidence.roi != layout.roi:
            return finish(("frame_evidence_roi_mismatch",))
        if ((source_kind == "synthetic" and evidence.assessment_method != "synthetic")
                or (source_kind != "synthetic" and evidence.assessment_method != "agent_development_observation")):
            return finish(("evidence_authority_mismatch",))
        blockers = []
        if evidence.layout_matches is not True:
            blockers.append("layout_mismatch" if evidence.layout_matches is False else "layout_identity_unknown")
        if evidence.visibility != "clear_candidate":
            blockers.append("occluded" if evidence.visibility == "occluded" else "visibility_unknown")
        if evidence.animation != "not_observed_candidate":
            blockers.append("animation_observed" if evidence.animation == "observed" else "animation_unknown")
        if blockers:
            return finish(blockers)
        if ocr is None:
            return finish(("ocr_unavailable",))
        variants = []
        with image.crop(layout.roi) as crop:
            for scale in layout.scales:
                variant_start = time.perf_counter()
                if crop.width * crop.height * scale * scale > MAX_IMAGE_PIXELS:
                    variants.append(_Variant(scale, reason="scaled_image_too_large",
                                             processing_seconds=time.perf_counter() - variant_start))
                    continue
                try:
                    with crop.resize((crop.width * scale, crop.height * scale), Image.Resampling.NEAREST) as scaled:
                        with io.BytesIO() as encoded:
                            scaled.save(encoded, format="PNG")
                            variant = _read_variant(ocr.recognize(encoded.getvalue()), layout, scale)
                except Exception:
                    # Backend messages may include image contents or paths.
                    variant = _Variant(scale, reason="ocr_failed")
                variants.append(replace(variant, processing_seconds=time.perf_counter() - variant_start))
        if any(v.pair is None for v in variants):
            reasons = tuple(dict.fromkeys(("variant_incomplete", *(v.reason for v in variants if v.reason))))
            return finish(reasons, variants)
        if any(v.pair != variants[0].pair for v in variants[1:]):
            return finish(("variant_disagreement",), variants)
        return finish(variants=variants, pair=variants[0].pair)
