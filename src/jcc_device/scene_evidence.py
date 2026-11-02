"""Offline scene hypotheses and frame-bound field evidence, never game state.

There is deliberately no ContextEvidence/FrameSceneEvidence exporter. Image
cues cannot certify visibility, ownership, human review or a continuous stream.
No real layout, TTL, device access, asset path or raw OCR text is shipped.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import io
import json
import math
import time
from typing import Sequence

from .latest import Frame, FrameRef
from .recognition import PixelROI, RawOCR
from .roi_recognition import (
    CANDIDATE, MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, NUMERIC_FIELDS, ImageGeometry,
    ROIExperiment, ROILayout, parse_numeric, probe_frame, reconcile_variants,
)


VERSION = "scene-evidence-v1"
CUE_IDS = ("round_header", "shop_experience", "shop_refresh", "level_label",
           "phase_preparation", "phase_combat", "phase_carousel", "phase_selection",
           "blocker_dialog", "blocker_loading", "blocker_settlement")
PHASES = ("preparation", "combat", "carousel", "selection", "transition")
VISIBILITY = ("clear", "occluded", "unknown")
SELF_ROLES = {"round": "global_round", "hp": "self_hp",
              "gold": "self_gold", "level": "self_level"}
ROLES = (*SELF_ROLES.values(), "observed_hp", "observed_gold", "observed_level",
         "opponent_hp", "opponent_gold", "opponent_level", "unknown")


def _name(value: str) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _integer(value: int) -> bool:
    return type(value) is int and value >= 0


def _regions(regions: Sequence[PixelROI], geometry: ImageGeometry) -> tuple[PixelROI, ...]:
    copied = tuple(PixelROI(r.name, tuple(r.image_size), tuple(r.box)) for r in regions)
    if not copied or len({r.name for r in copied}) != len(copied):
        raise ValueError("Nonempty, uniquely named full-frame regions are required")
    for region in copied:
        region.validate(geometry.image_size)
    return copied


@dataclass(frozen=True)
class LayoutKey:
    """Exact declared input conditions, not an automatic layout detector.

    None mode means UNKNOWN, never a verified default. Equal dimensions alone
    do not bind a screenshot to a video frame, or replay to live play.
    """

    layout_id: str = field(repr=False)
    geometry: ImageGeometry
    sample_kind: str
    capture_kind: str
    mode_key: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if (not _name(self.layout_id) or not isinstance(self.geometry, ImageGeometry)
                or self.sample_kind not in ("synthetic", "replay", "live")
                or self.capture_kind not in ("screenshot", "video_frame")
                or self.mode_key is not None and not _name(self.mode_key)):
            raise ValueError("Explicit layout, geometry and source conditions are required")


@dataclass(frozen=True)
class FrameBinding:
    source: FrameRef = field(repr=False)
    layout_key: LayoutKey
    host_clock_id: str = field(repr=False)
    context_epoch: int
    image_sha256: str | None = field(repr=False)

    def __post_init__(self):
        if (not isinstance(self.source, FrameRef) or not _name(self.source.stream_id)
                or not _integer(self.source.sequence)
                or not _integer(self.source.host_received_monotonic_ns)
                or not isinstance(self.layout_key, LayoutKey)
                or not _name(self.host_clock_id) or not _integer(self.context_epoch)):
            raise ValueError("A complete source, clock and context binding is required")
        if self.image_sha256 is not None and (
                not isinstance(self.image_sha256, str) or len(self.image_sha256) != 64
                or any(c not in "0123456789abcdef" for c in self.image_sha256)):
            raise ValueError("Invalid private image digest")


def bind_frame(frame: Frame, key: LayoutKey, *, host_clock_id: str, context_epoch: int) -> FrameBinding:
    """Bind EXACT encoded bytes; digest stays in memory and out of summaries.

    This is equality evidence, not proof of capture time, mode or device origin.
    Invalid/oversized inputs have no digest and can never authorize candidates.
    """
    digest = (hashlib.sha256(frame.jpeg).hexdigest()
              if isinstance(frame.jpeg, bytes) and 0 < len(frame.jpeg) <= MAX_IMAGE_BYTES else None)
    return FrameBinding(frame.source, key, host_clock_id, context_epoch, digest)


@dataclass(frozen=True)
class CueSpec:
    cue_id: str
    roi: PixelROI
    exact_texts: tuple[str, ...] = field(default=(), repr=False)
    scale: int = 1
    minimum_raw_score: float = 0.5

    def __post_init__(self):
        if self.cue_id not in CUE_IDS:
            raise ValueError("Unsupported cue code")
        texts = tuple(self.exact_texts)
        grammar = self.cue_id in ("round_header", "level_label")
        if (grammar and texts or not grammar and not texts
                or any(not _name(s) or s != s.strip() for s in texts)
                or len(set(texts)) != len(texts)):
            raise ValueError("Use explicit exact labels or the dedicated numeric grammar")
        if type(self.scale) is not int or not 1 <= self.scale <= 4:
            raise ValueError("Cue scale must be an integer from one to four")
        score = self.minimum_raw_score
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score) or not 0 <= score <= 1):
            raise ValueError("Invalid uncalibrated raw score filter")
        object.__setattr__(self, "exact_texts", texts)
        copied = _regions((self.roi,), ImageGeometry(self.roi.image_size))[0]
        object.__setattr__(self, "roi", copied)


@dataclass(frozen=True)
class SceneLayout:
    key: LayoutKey
    cues: tuple[CueSpec, ...]

    def __post_init__(self):
        cues = tuple(self.cues)
        if (not cues or any(not isinstance(c, CueSpec) for c in cues)
                or len({c.cue_id for c in cues}) != len(cues)):
            raise ValueError("Distinct explicit cues are required")
        _regions(tuple(c.roi for c in cues), self.key.geometry)
        if any((c.roi.box[2] - c.roi.box[0]) * (c.roi.box[3] - c.roi.box[1])
               * c.scale ** 2 > MAX_IMAGE_PIXELS for c in cues):
            raise ValueError("Scaled cue inputs exceed the pixel resource limit")
        object.__setattr__(self, "cues", cues)


@dataclass(frozen=True)
class CueEvidence:
    cue_id: str
    present: bool
    reason: str | None
    input_region: tuple[int, int, int, int]
    text_region: tuple[float, float, float, float] | None = None
    ocr_items_count: int = 0


@dataclass(frozen=True)
class Hypothesis:
    status: str = "unknown"
    value: str | None = field(default=None, repr=False)
    reason: str | None = None


@dataclass(frozen=True)
class SceneProbe:
    binding: FrameBinding = field(repr=False)
    cues: tuple[CueEvidence, ...]
    scene: Hypothesis
    phase: Hypothesis
    started_monotonic_ns: int
    completed_monotonic_ns: int
    failure_reason: str | None = None

    @property
    def dependencies(self) -> tuple[tuple[int, int, int, int], ...]:
        # ALL attempted inputs, including nonmatches and blocker searches.
        return tuple(dict.fromkeys(c.input_region for c in self.cues))

    def summary(self) -> dict:
        key = self.binding.layout_key
        return {"method": VERSION, "evidence": "DEVELOPMENT_ONLY_NOT_REVIEWED",
                "sample_kind": key.sample_kind, "capture_kind": key.capture_kind,
                "image_size": list(key.geometry.image_size),
                "rotation_applied_degrees": key.geometry.applied_rotation_degrees,
                "failure_reason": self.failure_reason,
                "scene": {"status": self.scene.status, "candidate": self.scene.value,
                          "reason": self.scene.reason},
                "phase": {"status": self.phase.status, "candidate": self.phase.value,
                          "reason": self.phase.reason},
                "cues": {c.cue_id: {"present": c.present, "reason": c.reason,
                                    "ocr_items_count": c.ocr_items_count} for c in self.cues},
                "processing_seconds": (self.completed_monotonic_ns - self.started_monotonic_ns) / 1e9,
                "recognized_count": 0, "accuracy": None, "full_m1_acceptance": "not_assessed"}


def _cue(spec: CueSpec, items, size: tuple[int, int]) -> CueEvidence:
    def result(reason, *, count=0, region=None):
        return CueEvidence(spec.cue_id, reason is None, reason, spec.roi.box, region, count)
    if not isinstance(items, list):
        return result("invalid_ocr_response")
    count = len(items)
    if count != 1:
        return result("no_text" if count == 0 else "ambiguous_ocr_items", count=count)
    item = items[0]
    try:
        text = item["text"]
        x, y, w, h, score = (item[k] for k in ("x", "y", "width", "height", "confidence"))
        if (not isinstance(text, str)
                or any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n)
                       for n in (x, y, w, h, score)) or not 0 <= score <= 1
                or not (0 <= x < x + w <= size[0] and 0 <= y < y + h <= size[1])):
            raise ValueError
    except (TypeError, KeyError, ValueError):
        return result("invalid_ocr_response", count=count)
    if x <= 0 or y <= 0 or x + w >= size[0] or y + h >= size[1]:
        return result("text_touches_crop_edge", count=count)
    if score < spec.minimum_raw_score:
        return result("low_raw_score", count=count)
    text = text.strip()
    if spec.cue_id == "round_header":
        matched = parse_numeric("round", text) is not None
    elif spec.cue_id == "level_label":
        matched = parse_numeric("level", text, syntax="level_label") is not None
    else:
        matched = text in spec.exact_texts
    if not matched:
        return result("unmatched_text", count=count)
    left, top, _, _ = spec.roi.box
    return result(None, count=count, region=(left + x / spec.scale, top + y / spec.scale,
                                            left + (x + w) / spec.scale, top + (y + h) / spec.scale))


def probe_scene(frame: Frame, *, key: LayoutKey, host_clock_id: str, context_epoch: int,
                layout: SceneLayout | None, ocr: RawOCR | None) -> SceneProbe:
    """Exact per-frame cue experiment; never claims clear visibility or self view.

    Shop labels + a round header support only a battle hypothesis. Preparation
    requires an explicit preparation cue; a shop/countdown/board is insufficient.
    A missed blocker is UNKNOWN, never evidence that no overlay exists.
    """
    from PIL import Image

    started = time.monotonic_ns()
    binding = bind_frame(frame, key, host_clock_id=host_clock_id, context_epoch=context_epoch)
    specs = layout.cues if layout else ()

    def finish(cues=(), failure=None):
        present = {c.cue_id for c in cues if c.present}
        reason = failure
        if not reason and any(c.startswith("blocker_") for c in present):
            reason = "blocking_cue_present"
        if reason:
            scene = phase = Hypothesis(reason=reason)
        else:
            battle = {"round_header", "shop_experience", "shop_refresh"} <= present
            scene = Hypothesis(CANDIDATE, "battle") if battle else Hypothesis(reason="battle_cues_incomplete")
            phases = tuple(p for p in PHASES if "phase_" + p in present)
            phase = (Hypothesis(reason="battle_scene_required") if not battle else
                     Hypothesis(reason="phase_cue_conflict") if len(phases) > 1 else
                     Hypothesis(CANDIDATE, phases[0]) if phases else
                     Hypothesis(reason="explicit_phase_cue_missing"))
        return SceneProbe(binding, tuple(cues), scene, phase, started, time.monotonic_ns(), failure)

    def fail(reason):
        return finish(tuple(CueEvidence(s.cue_id, False, reason, s.roi.box) for s in specs), reason)

    if layout is None:
        return fail("layout_unconfigured")
    if layout.key != key:
        return fail("layout_mismatch")
    if binding.image_sha256 is None:
        return fail("image_unreadable")
    try:
        with Image.open(io.BytesIO(frame.jpeg)) as original:
            if original.format not in ("PNG", "JPEG") or getattr(original, "n_frames", 1) != 1:
                return fail("unsupported_image")
            if original.getexif().get(274, 1) != 1:
                return fail("unapplied_exif_rotation")
            if original.size != key.geometry.image_size or original.width * original.height > MAX_IMAGE_PIXELS:
                return fail("image_geometry_mismatch")
            image = original.convert("RGB")
    except Exception:
        return fail("image_unreadable")
    cues = []
    with image:
        for spec in specs:
            if ocr is None:
                cues.append(CueEvidence(spec.cue_id, False, "ocr_unavailable", spec.roi.box))
                continue
            try:
                with image.crop(spec.roi.box) as crop, io.BytesIO() as output:
                    size = (crop.width * spec.scale, crop.height * spec.scale)
                    with crop.resize(size, Image.Resampling.NEAREST) as scaled:
                        scaled.save(output, format="PNG")
                    evidence = _cue(spec, ocr.recognize(output.getvalue()), size)
            except Exception:
                evidence = CueEvidence(spec.cue_id, False, "ocr_failed", spec.roi.box)
            cues.append(evidence)
    return finish(cues)


@dataclass(frozen=True)
class BoundNumbers:
    binding: FrameBinding = field(repr=False)
    experiment: ROIExperiment = field(repr=False)
    started_monotonic_ns: int
    completed_monotonic_ns: int

    def __post_init__(self):
        exp, key = self.experiment, self.binding.layout_key
        if (exp.source != self.binding.source or exp.geometry != key.geometry
                or exp.sample_kind != key.sample_kind or exp.layout is None
                or exp.layout.layout_id != key.layout_id
                or tuple(p.field_name for p in exp.probes) != NUMERIC_FIELDS):
            raise ValueError("Numeric experiment and exact input binding disagree")


def probe_numbers(frame: Frame, *, key: LayoutKey, host_clock_id: str, context_epoch: int,
                  layouts: Sequence[ROILayout], ocr: RawOCR | None) -> BoundNumbers:
    """Wrap the unchanged 10F dependency with exact-byte/clock/context binding.

    An already computed ROIExperiment has no byte digest. Do not retrospectively
    attach it to some other image. Run this wrapper on the actual same Frame.
    """
    layouts = tuple(layouts)
    if not layouts or any((layout.layout_id, layout.geometry, layout.sample_kind) !=
                          (key.layout_id, key.geometry, key.sample_kind) for layout in layouts):
        raise ValueError("Numeric layouts must match the explicit input conditions")
    if any((f.roi.box[2] - f.roi.box[0]) * (f.roi.box[3] - f.roi.box[1])
           * f.scale ** 2 > MAX_IMAGE_PIXELS for layout in layouts for f in layout.fields):
        raise ValueError("Scaled numeric inputs exceed the pixel resource limit")
    started = time.monotonic_ns()
    binding = bind_frame(frame, key, host_clock_id=host_clock_id, context_epoch=context_epoch)
    variants = tuple(probe_frame(frame, geometry=key.geometry, sample_kind=key.sample_kind,
                                layout=layout, ocr=ocr) for layout in layouts)
    combined = variants[0] if len(variants) == 1 else reconcile_variants(variants)
    return BoundNumbers(binding, combined, started, time.monotonic_ns())


@dataclass(frozen=True)
class FieldWitness:
    field_name: str
    role: str
    visibility: str
    regions: tuple[PixelROI, ...]

    def __post_init__(self):
        if self.field_name not in NUMERIC_FIELDS or self.role not in ROLES or self.visibility not in VISIBILITY:
            raise ValueError("Explicit field role and visibility are required")
        regions = tuple(self.regions)
        if not regions:
            raise ValueError("Explicit field verifier input coverage is required")
        object.__setattr__(self, "regions", _regions(regions, ImageGeometry(regions[0].image_size)))


@dataclass(frozen=True)
class FrameWitness:
    """External per-frame ASSERTION, not automatically authenticated review.

    Machine/agent judgements MUST use development_candidate. Human review can
    only be asserted by a real process with traceable independent evidence.
    coverage_regions declare ALL pixels used by that external process.
    """

    binding: FrameBinding = field(repr=False)
    scene: str | None
    phase: str | None
    visibility: str
    review_status: str
    verification_ref: str = field(repr=False)
    coverage_regions: tuple[PixelROI, ...]
    fields: tuple[FieldWitness, ...]
    completed_monotonic_ns: int
    expires_monotonic_ns: int

    def __post_init__(self):
        if (self.scene not in (None, "battle") or self.phase not in (None, *PHASES)
                or self.visibility not in VISIBILITY
                or self.review_status not in ("synthetic", CANDIDATE, "independently_reviewed")
                or not _name(self.verification_ref)
                or not _integer(self.completed_monotonic_ns) or not _integer(self.expires_monotonic_ns)
                or not self.binding.source.host_received_monotonic_ns <= self.completed_monotonic_ns
                < self.expires_monotonic_ns):
            raise ValueError("Explicit per-frame witness provenance and original deadline are required")
        geometry = self.binding.layout_key.geometry
        object.__setattr__(self, "coverage_regions", _regions(self.coverage_regions, geometry))
        fields = tuple(self.fields)
        if any(not isinstance(f, FieldWitness) for f in fields) or len({f.field_name for f in fields}) != len(fields):
            raise ValueError("Field witnesses must be unique")
        for f in fields:
            _regions(f.regions, geometry)
        object.__setattr__(self, "fields", fields)


@dataclass(frozen=True)
class ClockReading:
    host_clock_id: str = field(repr=False)
    monotonic_ns: int

    def __post_init__(self):
        if not _name(self.host_clock_id) or not _integer(self.monotonic_ns):
            raise ValueError("An explicit same-domain monotonic clock reading is required")


@dataclass(frozen=True)
class FieldDecision:
    field_name: str
    status: str = "unknown"
    candidate_value: int | dict | None = field(default=None, repr=False)
    reason: str | None = None
    input_regions: tuple[tuple[int, int, int, int], ...] = ()
    normalized_regions: tuple[tuple[float, float, float, float], ...] = ()


def _covers(target, regions) -> bool:
    """Exact union coverage of axis-aligned rectangles; gaps never count."""
    x1, y1, x2, y2 = target
    clipped = [(max(x1, a), max(y1, b), min(x2, c), min(y2, d)) for a, b, c, d in regions
               if a < x2 and c > x1 and b < y2 and d > y1]
    xs = sorted({x1, x2, *(x for a, _, c, _ in clipped for x in (a, c))})
    for left, right in zip(xs, xs[1:]):
        covered_to = y1
        for low, high in sorted((b, d) for a, b, c, d in clipped if a <= left and c >= right):
            if low > covered_to:
                return False
            covered_to = max(covered_to, high)
        if covered_to < y2:
            return False
    return True


def assess_fields(scene: SceneProbe, numbers: BoundNumbers, *, current: FrameBinding,
                  now: ClockReading, max_frame_age_ns: int,
                  witness: FrameWitness | None = None) -> tuple[FieldDecision, ...]:
    """Candidate-only gate; exact frame, original deadline and complete inputs.

    max_frame_age_ns is REQUIRED caller policy, not a calibrated recommendation.
    No merging, continuity, not_applicable inference, state TTL or actions.
    """
    if type(max_frame_age_ns) is not int or max_frame_age_ns <= 0:
        raise ValueError("An explicit positive frame age policy is required")
    observed = current.source.host_received_monotonic_ns
    reason = None
    if scene.binding != current or numbers.binding != current:
        reason = "frame_binding_mismatch"
    elif current.image_sha256 is None:
        reason = "image_unreadable"
    elif now.host_clock_id != current.host_clock_id:
        reason = "clock_domain_mismatch"
    elif not (observed <= scene.started_monotonic_ns <= scene.completed_monotonic_ns <= now.monotonic_ns
              and observed <= numbers.started_monotonic_ns <= numbers.completed_monotonic_ns <= now.monotonic_ns):
        reason = "invalid_processing_time"
    elif now.monotonic_ns >= observed + max_frame_age_ns:
        reason = "frame_expired"
    elif scene.failure_reason or numbers.experiment.failure_reason:
        reason = scene.failure_reason or numbers.experiment.failure_reason
    elif witness is None:
        reason = "frame_witness_missing"
    elif witness.binding != current:
        reason = "witness_binding_mismatch"
    elif (current.layout_key.sample_kind == "synthetic" and witness.review_status != "synthetic"
          or current.layout_key.sample_kind != "synthetic" and witness.review_status != "independently_reviewed"):
        reason = "frame_witness_unreviewed"
    elif witness.completed_monotonic_ns > now.monotonic_ns:
        reason = "witness_from_future"
    elif now.monotonic_ns >= witness.expires_monotonic_ns:
        reason = "evidence_expired"
    elif witness.visibility != "clear":
        reason = "occluded" if witness.visibility == "occluded" else "visibility_unknown"
    elif scene.scene.status != CANDIDATE or scene.scene.value != "battle" or witness.scene != "battle":
        reason = "battle_scene_unproven"
    elif witness.phase not in ("preparation", "combat"):
        reason = "phase_unproven_or_unsupported"
    elif scene.phase.value is not None and scene.phase.value != witness.phase:
        reason = "phase_disagreement"
    elif scene.phase.reason == "phase_cue_conflict":
        reason = "phase_cue_conflict"
    elif not all(_covers(box, tuple(r.box for r in witness.coverage_regions)) for box in scene.dependencies):
        reason = "scene_input_coverage_incomplete"
    if reason:
        return tuple(FieldDecision(name, reason=reason) for name in NUMERIC_FIELDS)

    by_field = {f.field_name: f for f in witness.fields}
    width, height = current.layout_key.geometry.image_size
    decisions = []
    for probe in numbers.experiment.probes:
        f = by_field.get(probe.field_name)
        reason = None
        if f is None:
            reason = "field_identity_missing"
        elif f.role != SELF_ROLES[probe.field_name]:
            reason = "field_subject_unproven"
        elif f.visibility != "clear":
            reason = "field_occluded" if f.visibility == "occluded" else "field_visibility_unknown"
        elif probe.input_region is None or not _covers(probe.input_region, tuple(r.box for r in f.regions)):
            reason = "field_input_coverage_incomplete"
        elif probe.status != CANDIDATE:
            reason = probe.reason
        boxes = tuple(dict.fromkeys((*scene.dependencies,
                      *(r.box for r in witness.coverage_regions),
                      *((probe.input_region,) if probe.input_region else ()),
                      *((r.box for r in f.regions) if f else ()))))
        normalized = tuple((a / width, b / height, c / width, d / height) for a, b, c, d in boxes)
        decisions.append(FieldDecision(probe.field_name, "unknown" if reason else CANDIDATE,
                                       None if reason else probe.candidate_value, reason, boxes, normalized))
    return tuple(decisions)


def summarize_fields(decisions: Sequence[FieldDecision]) -> dict:
    return {"method": VERSION, "fields": {d.field_name: {"status": d.status, "reason": d.reason}
                                          for d in decisions},
            "recognized_count": 0, "accuracy": None, "full_m1_acceptance": "not_assessed"}


def synthetic_demo() -> dict:
    """Actual existing Apple Vision on generated labels, only in memory."""
    from PIL import Image, ImageDraw, ImageFont
    from .vision_ocr import VisionOCR

    geometry = ImageGeometry((960, 640))
    key = LayoutKey("synthetic-scene-demo", geometry, "synthetic", "screenshot")
    boxes = (("round_header", (20, 20, 210, 100), ()),
             ("shop_experience", (20, 500, 350, 590), ("BUY XP",)),
             ("shop_refresh", (400, 500, 900, 590), ("REFRESH",)),
             ("phase_preparation", (240, 20, 900, 100), ("PREPARATION",)),
             ("phase_combat", (240, 120, 900, 210), ("COMBAT",)),
             ("blocker_dialog", (240, 250, 900, 340), ("SETTINGS",)))
    layout = SceneLayout(key, tuple(CueSpec(code, PixelROI(code, geometry.image_size, box), texts)
                                   for code, box, texts in boxes))
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 40)
    cases = (("preparation", ("3-2", "BUY XP", "REFRESH", "PREPARATION", "", "")),
             ("combat_with_shop", ("3-2", "BUY XP", "REFRESH", "", "COMBAT", "")),
             ("phase_conflict", ("3-2", "BUY XP", "REFRESH", "PREPARATION", "COMBAT", "")),
             ("numbers_only", ("3-2", "", "", "", "", "")),
             ("dialog_with_background", ("3-2", "BUY XP", "REFRESH", "PREPARATION", "", "SETTINGS")),
             ("no_phase_label", ("3-2", "BUY XP", "REFRESH", "", "", "")))
    reports = []
    with VisionOCR() as ocr:
        for index, (code, texts) in enumerate(cases, 1):
            with Image.new("RGB", geometry.image_size, "black") as image, io.BytesIO() as output:
                draw = ImageDraw.Draw(image)
                for (_, box, _), text in zip(boxes, texts):
                    draw.text((box[0] + 15, box[1] + 15), text, fill="white", font=font)
                image.save(output, format="PNG")
                frame = Frame(FrameRef("synthetic-only", index, time.monotonic_ns(), "synthetic"), output.getvalue())
            result = probe_scene(frame, key=key, host_clock_id="synthetic-demo-clock", context_epoch=0,
                                 layout=layout, ocr=ocr)
            reports.append({"synthetic_case": code, **result.summary()})
    return {"method": VERSION, "evidence": "SYNTHETIC_TOOL_EXPERIMENT_NOT_REAL_RESULTS",
            "cases": reports, "real_frames": 0, "accuracy": None}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline candidate scene evidence; no device access or real layout")
    parser.add_argument("--synthetic-demo", action="store_true", help="Run existing local OCR on in-memory synthetic labels")
    args = parser.parse_args(argv)
    if not args.synthetic_demo:
        parser.print_help()
        return 0
    try:
        report = synthetic_demo()
    except Exception:
        print(json.dumps({"status": "no_results", "reason": "synthetic_demo_failed", "accuracy": None}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
