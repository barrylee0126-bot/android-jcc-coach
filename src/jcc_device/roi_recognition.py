"""Offline, opt-in ROI experiments; never publishes game-state observations.

Numeric probes are DEVELOPMENT CANDIDATES, not independently reviewed labels.
Interpretation also requires explicit per-frame scene/visibility evidence. No
default game coordinates, implicit resizing, device access, or result reuse.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import io
import json
import math
from pathlib import Path
import re
import time
from typing import Sequence

from .latest import Frame, FrameRef
from .recognition import PixelROI, RawOCR


VERSION = "roi-recognition-v1"
NUMERIC_FIELDS = ("round", "hp", "gold", "level")
FIELDS = ("scene", *NUMERIC_FIELDS)
SCENES = ("lobby", "mode_select", "battle", "loading", "settlement", "history",
          "options_dialog", "other")
COORDINATE_SPACE = "decoded_frame_normalized_top_left"
CANDIDATE = "development_candidate"
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000


@dataclass(frozen=True)
class ImageGeometry:
    """Geometry of ALREADY decoded/rotated full-frame pixels; no auto rotation."""

    image_size: tuple[int, int]
    applied_rotation_degrees: int = 0

    def __post_init__(self) -> None:
        size = tuple(self.image_size)
        if (len(size) != 2 or any(type(n) is not int or n <= 0 for n in size)
                or type(self.applied_rotation_degrees) is not int
                or self.applied_rotation_degrees not in (0, 90, 180, 270)):
            raise ValueError("Explicit positive dimensions and applied rotation are required")
        object.__setattr__(self, "image_size", size)

    @property
    def orientation(self) -> str:
        width, height = self.image_size
        return "landscape" if width > height else "portrait" if width < height else "square"


@dataclass(frozen=True)
class FieldROI:
    """The WHOLE input to one OCR call, before integer nearest-neighbor scaling.

    bounds, when supplied, are caller-reviewed mode rules, not game defaults.
    A raw OCR score threshold is an engineering filter, never a probability.
    """

    field_name: str
    roi: PixelROI
    scale: int = 1
    minimum_raw_score: float = 0.5
    bounds: tuple[int, int] | None = None
    syntax: str = "bare"

    def __post_init__(self) -> None:
        if self.field_name not in NUMERIC_FIELDS:
            raise ValueError("Only round, hp, gold and level numeric probes are supported")
        if self.syntax not in ("bare", "level_label") or self.syntax == "level_label" and self.field_name != "level":
            raise ValueError("The explicit level_label grammar is available only for level")
        if type(self.scale) is not int or not 1 <= self.scale <= 4:
            raise ValueError("OCR scale must be an integer from one to four")
        score = self.minimum_raw_score
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score) or not 0 <= score <= 1):
            raise ValueError("Raw score threshold must be finite and within zero and one")
        copied = PixelROI(self.roi.name, tuple(self.roi.image_size), tuple(self.roi.box))
        copied.validate(copied.image_size)
        object.__setattr__(self, "roi", copied)
        if self.bounds is not None:
            bounds = tuple(self.bounds)
            if (self.field_name == "round" or len(bounds) != 2
                    or any(type(n) is not int for n in bounds) or bounds[0] > bounds[1]):
                raise ValueError("Explicit integer bounds are allowed only for scalar fields")
            object.__setattr__(self, "bounds", bounds)


@dataclass(frozen=True)
class ROILayout:
    """A declared experimental layout; no shipped real-device calibration."""

    layout_id: str
    geometry: ImageGeometry
    sample_kind: str
    fields: tuple[FieldROI, ...]

    def __post_init__(self) -> None:
        if (not isinstance(self.layout_id, str) or not self.layout_id.strip()
                or self.sample_kind not in ("synthetic", "replay", "live")
                or not isinstance(self.geometry, ImageGeometry)):
            raise ValueError("Explicit layout identity, geometry and sample kind are required")
        fields = tuple(self.fields)
        if not fields or any(not isinstance(item, FieldROI) for item in fields):
            raise ValueError("A layout requires numeric field ROIs")
        if (len({item.field_name for item in fields}) != len(fields)
                or len({item.roi.name for item in fields}) != len(fields)):
            raise ValueError("Field and ROI names must be unique")
        for item in fields:
            item.roi.validate(self.geometry.image_size)
        object.__setattr__(self, "fields", fields)


@dataclass(frozen=True)
class NumericProbe:
    """Syntax-only experiment. Even a candidate may read the wrong UI entity."""

    field_name: str
    status: str
    candidate_value: int | dict | None = field(default=None, repr=False)
    reason: str | None = None
    input_region: tuple[int, int, int, int] | None = None
    text_region: tuple[float, float, float, float] | None = None
    raw_score: float | None = None
    ocr_items_count: int = 0
    processing_seconds: float = 0.0


@dataclass(frozen=True)
class ROIExperiment:
    source: FrameRef
    geometry: ImageGeometry
    layout: ROILayout | None
    sample_kind: str
    probes: tuple[NumericProbe, ...]
    processing_seconds: float
    failure_reason: str | None = None
    variant_count: int = 1

    def summary(self) -> dict:
        """Fixed codes/counts only: no values, OCR strings, identities or images."""
        return {"method": VERSION, "sample_kind": self.sample_kind,
                "evidence": "SYNTHETIC_NOT_REAL" if self.sample_kind == "synthetic"
                else "DEVELOPMENT_CANDIDATES_REQUIRE_INDEPENDENT_HUMAN_REVIEW",
                "image_size": list(self.geometry.image_size),
                "orientation": self.geometry.orientation,
                "rotation_applied_degrees": self.geometry.applied_rotation_degrees,
                "coordinate_space": COORDINATE_SPACE,
                "processing_seconds": self.processing_seconds,
                "failure_reason": self.failure_reason,
                "variant_count": self.variant_count,
                "fields": {item.field_name: {"status": item.status, "reason": item.reason,
                            "ocr_items_count": item.ocr_items_count,
                            "processing_seconds": item.processing_seconds}
                           for item in self.probes},
                "accuracy": None, "full_m1_acceptance": "not_assessed"}


def _unknown(name: str, reason: str, config: FieldROI | None = None, **kwargs) -> NumericProbe:
    return NumericProbe(name, "unknown", reason=reason,
                        input_region=config.roi.box if config else None, **kwargs)


def parse_numeric(field_name: str, text: str, bounds: tuple[int, int] | None = None, *, syntax: str = "bare"):
    """Return a typed syntax candidate or None. No glyph repair or digit joining.

    Signed hp/gold follow the contract, without assumed mode-specific limits.
    Twelve digits are a resource limit, not a verified game rule.
    """
    if field_name not in NUMERIC_FIELDS:
        raise ValueError("Unsupported numeric field")
    if syntax not in ("bare", "level_label") or syntax == "level_label" and field_name != "level":
        raise ValueError("Unsupported numeric grammar")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if syntax == "level_label":
        match = re.fullmatch(r"([1-9][0-9]{0,11})级", text)
        if not match:
            return None
        text = match[1]
    if field_name == "round":
        match = re.fullmatch(r"([1-9][0-9]{0,11})-([1-9][0-9]{0,11})", text)
        return {"stage": int(match[1]), "step": int(match[2])} if match else None
    if not re.fullmatch(r"-?(?:0|[1-9][0-9]{0,11})", text) or text == "-0":
        return None
    value = int(text)
    if field_name == "level" and value <= 0:
        return None
    if bounds is not None and not bounds[0] <= value <= bounds[1]:
        return None
    return value


def _numeric_result(config: FieldROI, items, size: tuple[int, int]) -> NumericProbe:
    if not isinstance(items, list):
        return _unknown(config.field_name, "invalid_ocr_response", config)
    count = len(items)
    if not items:
        return _unknown(config.field_name, "no_text", config)
    if count != 1:
        return _unknown(config.field_name, "ambiguous_ocr_items", config, ocr_items_count=count)
    item = items[0]
    if not isinstance(item, dict) or not isinstance(item.get("text"), str):
        return _unknown(config.field_name, "invalid_ocr_response", config, ocr_items_count=count)
    try:
        x, y, width, height, score = (item[key] for key in ("x", "y", "width", "height", "confidence"))
        if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n)
               for n in (x, y, width, height, score)) or not 0 <= score <= 1:
            raise ValueError
        if not (0 <= x < x + width <= size[0] and 0 <= y < y + height <= size[1]):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return _unknown(config.field_name, "invalid_ocr_response", config, ocr_items_count=count)
    # Text touching a crop boundary may have lost a digit. This does not detect
    # every clipped glyph, and is not proof of visibility or crop sufficiency.
    if x <= 0 or y <= 0 or x + width >= size[0] or y + height >= size[1]:
        return _unknown(config.field_name, "text_touches_crop_edge", config, ocr_items_count=count)
    if score < config.minimum_raw_score:
        return _unknown(config.field_name, "low_raw_score", config, raw_score=score, ocr_items_count=count)
    value = parse_numeric(config.field_name, item["text"], config.bounds, syntax=config.syntax)
    if value is None:
        return _unknown(config.field_name, "invalid_numeric_syntax_or_bounds", config,
                        raw_score=score, ocr_items_count=count)
    left, top, _, _ = config.roi.box
    box = (left + x / config.scale, top + y / config.scale,
           left + (x + width) / config.scale, top + (y + height) / config.scale)
    return NumericProbe(config.field_name, CANDIDATE, value, input_region=config.roi.box,
                        text_region=box, raw_score=score, ocr_items_count=count)


def probe_frame(frame: Frame, *, geometry: ImageGeometry, sample_kind: str,
                layout: ROILayout | None, ocr: RawOCR | None) -> ROIExperiment:
    """Decode once; lossless crop/optional integer scale; four independent probes.

    This ignores scene semantics on purpose: numeric candidates are useful for
    calibration ONLY. Use interpret() to apply the separate context gate.
    Neither function produces a game-state envelope or a continuity proof.
    """
    from dataclasses import replace
    from PIL import Image, UnidentifiedImageError

    if not isinstance(geometry, ImageGeometry) or sample_kind not in ("synthetic", "replay", "live"):
        raise ValueError("Explicit frame geometry and sample kind are required")
    started = time.perf_counter()
    configured = {item.field_name: item for item in layout.fields} if layout else {}

    def finish(probes, failure_reason=None):
        return ROIExperiment(frame.source, geometry, layout, sample_kind, tuple(probes),
                             time.perf_counter() - started, failure_reason)

    def fail(reason):
        return finish((_unknown(name, reason, configured.get(name)) for name in NUMERIC_FIELDS), reason)

    if layout is None:
        return fail("layout_unconfigured")
    if layout.geometry != geometry or layout.sample_kind != sample_kind:
        return fail("layout_mismatch")
    if not isinstance(frame.jpeg, bytes) or not 0 < len(frame.jpeg) <= MAX_IMAGE_BYTES:
        return fail("image_unreadable")
    try:
        with Image.open(io.BytesIO(frame.jpeg)) as original:
            if original.format not in ("PNG", "JPEG") or getattr(original, "n_frames", 1) != 1:
                return fail("unsupported_image")
            if original.getexif().get(274, 1) != 1:
                return fail("unapplied_exif_rotation")
            if original.size != geometry.image_size:
                return fail("image_geometry_mismatch")
            if original.width * original.height > MAX_IMAGE_PIXELS:
                return fail("image_too_large")
            original.load()
            image = original.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return fail("image_unreadable")
    with image:
        probes = []
        for name in NUMERIC_FIELDS:
            config = configured.get(name)
            if config is None:
                probes.append(_unknown(name, "field_unconfigured"))
                continue
            if ocr is None:
                probes.append(_unknown(name, "ocr_unavailable", config))
                continue
            field_started = time.perf_counter()
            try:
                with image.crop(config.roi.box) as crop:
                    size = (crop.width * config.scale, crop.height * config.scale)
                    with crop.resize(size, Image.Resampling.NEAREST) as scaled, io.BytesIO() as encoded:
                        scaled.save(encoded, format="PNG")
                        result = _numeric_result(config, ocr.recognize(encoded.getvalue()), size)
            except Exception:
                # Backend exceptions may contain OCR text or local paths.
                result = _unknown(name, "ocr_failed", config)
            probes.append(replace(result, processing_seconds=time.perf_counter() - field_started))
    return finish(probes)


def reconcile_variants(experiments: Sequence[ROIExperiment]) -> ROIExperiment:
    """Compare correlated scale variants of ONE source; disagreement is unknown.

    Agreement is still only a development candidate, not independent review.
    This does not pick the highest score or fill missing answers from a peer.
    """
    from dataclasses import replace

    variants = tuple(experiments)
    if len(variants) < 2:
        raise ValueError("At least two explicit variants are required")
    first = variants[0]
    reason = None
    if any((item.source, item.geometry, item.sample_kind) !=
           (first.source, first.geometry, first.sample_kind) for item in variants):
        reason = "variant_source_mismatch"
    elif any(item.failure_reason or item.layout is None for item in variants):
        reason = "variant_unusable"
    else:
        # Only scale/score filters may differ. No implicit crop or grammar
        # substitution is accepted as a comparable calibration experiment.
        signatures = [tuple((config.field_name, config.roi, config.syntax, config.bounds)
                            for config in item.layout.fields) for item in variants]
        if (any(item.layout.layout_id != first.layout.layout_id for item in variants)
                or any(signature != signatures[0] for signature in signatures[1:])):
            reason = "variant_layout_mismatch"
    if reason:
        probes = tuple(_unknown(name, reason) for name in NUMERIC_FIELDS)
    else:
        probes = []
        for index, name in enumerate(NUMERIC_FIELDS):
            group = [item.probes[index] for item in variants]
            if any(item.status != CANDIDATE for item in group):
                result = replace(group[0], status="unknown", candidate_value=None,
                                 text_region=None, raw_score=None, reason="variant_incomplete")
            elif any(item.candidate_value != group[0].candidate_value for item in group[1:]):
                result = replace(group[0], status="unknown", candidate_value=None,
                                 text_region=None, raw_score=None, reason="variant_disagreement")
            else:
                result = group[0]
            probes.append(replace(result, processing_seconds=sum(item.processing_seconds for item in group)))
        probes = tuple(probes)
    return replace(first, probes=probes, failure_reason=reason,
                   processing_seconds=sum(item.processing_seconds for item in variants),
                   variant_count=sum(item.variant_count for item in variants))


@dataclass(frozen=True)
class FrameSceneEvidence:
    """Caller assertion for this frame; code cannot authenticate human review.

    context_regions must cover ALL pixels used by the external verifier,
    including dialog/occlusion checks. Whole-frame OCR needs a whole-frame ROI.
    Real development guesses must use development_candidate and are rejected
    by interpret(); synthetic evidence never authorizes a real frame.
    """

    source: FrameRef
    geometry: ImageGeometry
    sample_kind: str
    layout_id: str
    scene: str | None
    unobscured: bool | None
    review_status: str
    verification_ref: str
    context_regions: tuple[PixelROI, ...]
    verified_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (not isinstance(self.geometry, ImageGeometry)
                or self.sample_kind not in ("synthetic", "replay", "live")
                or self.scene not in (None, *SCENES)
                or self.unobscured is not None and type(self.unobscured) is not bool
                or self.review_status not in ("synthetic", CANDIDATE, "independently_reviewed")
                or not self.layout_id or not self.verification_ref):
            raise ValueError("Explicit scene evidence provenance and review status are required")
        regions = tuple(PixelROI(roi.name, tuple(roi.image_size), tuple(roi.box))
                        for roi in self.context_regions)
        if not regions or len({roi.name for roi in regions}) != len(regions):
            raise ValueError("Explicit, uniquely named context coverage is required")
        for roi in regions:
            roi.validate(self.geometry.image_size)
        object.__setattr__(self, "context_regions", regions)
        verified_fields = tuple(self.verified_fields)
        if (len(set(verified_fields)) != len(verified_fields)
                or any(name not in NUMERIC_FIELDS for name in verified_fields)):
            raise ValueError("Verified field roles must name distinct supported numeric fields")
        object.__setattr__(self, "verified_fields", verified_fields)


@dataclass(frozen=True)
class LocalObservation:
    field_name: str
    status: str = "unknown"
    value: int | dict | str | None = field(default=None, repr=False)
    reason: str | None = None
    # Every dependency, not just the returned text bounding box. Full-frame
    # normalized coordinates use this observation's source pixels.
    input_regions: tuple[tuple[int, int, int, int], ...] = ()
    normalized_regions: tuple[tuple[float, float, float, float], ...] = ()


def interpret(experiment: ROIExperiment, context: FrameSceneEvidence | None = None) -> tuple[LocalObservation, ...]:
    """Abstain without matching scene/visibility evidence; never return recognized.

    An independently reviewed context still does not independently review OCR
    answers. Successful interpretations remain DEVELOPMENT CANDIDATES.
    No not_applicable deduction, TTL assignment, state merge, or reuse occurs.
    """
    layout = experiment.layout
    reason = None
    if experiment.failure_reason:
        reason = experiment.failure_reason
    elif layout is None:
        reason = "layout_unconfigured"
    elif context is None:
        reason = "scene_evidence_missing"
    elif (context.source != experiment.source or context.geometry != experiment.geometry
          or context.sample_kind != experiment.sample_kind or context.layout_id != layout.layout_id):
        reason = "scene_evidence_mismatch"
    elif ((experiment.sample_kind == "synthetic" and context.review_status != "synthetic")
          or (experiment.sample_kind != "synthetic" and context.review_status != "independently_reviewed")):
        reason = "scene_evidence_unreviewed"
    elif context.scene is None:
        reason = "scene_unknown"
    elif context.unobscured is not True:
        reason = "occluded" if context.unobscured is False else "visibility_unknown"
    if reason:
        return tuple(LocalObservation(name, reason=reason) for name in FIELDS)
    context_boxes = tuple(roi.box for roi in context.context_regions)
    width, height = experiment.geometry.image_size

    def candidate(name, value, boxes):
        normalized = tuple((x1 / width, y1 / height, x2 / width, y2 / height)
                           for x1, y1, x2, y2 in boxes)
        return LocalObservation(name, CANDIDATE, value, input_regions=boxes, normalized_regions=normalized)

    observations = [candidate("scene", context.scene, context_boxes)]
    for probe in experiment.probes:
        if context.scene != "battle":
            observations.append(LocalObservation(probe.field_name, reason="battle_scene_required"))
        elif probe.field_name not in context.verified_fields:
            observations.append(LocalObservation(probe.field_name, reason="field_identity_unverified"))
        elif probe.status != CANDIDATE:
            observations.append(LocalObservation(probe.field_name, reason=probe.reason))
        else:
            # Do not shrink the OCR input to its text bounding box. A blocker
            # elsewhere in that crop can affect the result too.
            observations.append(candidate(probe.field_name, probe.candidate_value,
                                          (probe.input_region, *context_boxes)))
    return tuple(observations)


def synthetic_demo() -> dict:
    """Actual Apple Vision on generated pixels in memory. No device or files."""
    from datetime import datetime, timezone
    from PIL import Image, ImageDraw, ImageFont
    from .vision_ocr import VisionOCR

    font_path = Path("/System/Library/Fonts/Supplemental/Arial.ttf")
    if not font_path.is_file():
        raise RuntimeError("synthetic_font_unavailable")
    font = ImageFont.truetype(str(font_path), 48)
    cases = (("round", "3-2"), ("hp", "100"), ("gold", "0"), ("level", "7"),
             ("hp", "-5"), ("gold", "42/50"), ("gold", "O8"), ("round", "3-0"),
             ("level", "0"), ("hp", ""), ("gold", "42\n50"))
    geometry = ImageGeometry((640, 360))
    reports = []
    with VisionOCR() as ocr:
        for index, (name, text) in enumerate(cases, 1):
            with Image.new("RGB", geometry.image_size, "black") as image, io.BytesIO() as out:
                ImageDraw.Draw(image).multiline_text((80, 80), text, fill="white", font=font)
                image.save(out, format="PNG")
                frame = Frame(FrameRef("synthetic-roi-demo", index, time.monotonic_ns(),
                              datetime.now(timezone.utc).isoformat()), out.getvalue())
            config = FieldROI(name, PixelROI("synthetic-value", geometry.image_size, (40, 40, 360, 260)))
            layout = ROILayout("synthetic-demo", geometry, "synthetic", (config,))
            result = probe_frame(frame, geometry=geometry, sample_kind="synthetic", layout=layout, ocr=ocr)
            probe = next(item for item in result.probes if item.field_name == name)
            expected = parse_numeric(name, text)
            outcome = ("abstained" if probe.status == "unknown" else
                       "candidate_matches_synthetic_input" if probe.candidate_value == expected
                       else "candidate_differs_from_synthetic_input")
            reports.append({"synthetic_case": index, "field": name, "outcome": outcome,
                            "status": probe.status, "reason": probe.reason,
                            "processing_seconds": result.processing_seconds})
    return {"method": VERSION, "evidence": "SYNTHETIC_TOOL_EXPERIMENT_NOT_REAL_RESULTS",
            "cases": reports, "real_frames": 0, "accuracy": None,
            "full_m1_acceptance": "not_assessed"}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="In-memory synthetic ROI OCR experiment; never accesses a device.")
    parser.add_argument("--synthetic-demo", action="store_true", help="Run local Apple Vision on generated numeric crops")
    args = parser.parse_args(argv)
    if not args.synthetic_demo:
        parser.print_help()
        return 0
    try:
        report = synthetic_demo()
    except Exception:
        # No supplied image/path/OCR contents or backend exception text.
        print(json.dumps({"status": "no_results", "reason": "synthetic_demo_failed", "accuracy": None}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
