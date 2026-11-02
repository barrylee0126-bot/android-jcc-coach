"""Small, local OCR probe with fixed slots and explicit uncertainty.

Layouts and vocabulary are supplied by the caller. No real-image configuration,
device API, model download, sample write, or strategy decision lives here.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, Sequence

from PIL import Image, ImageStat


class OCR(Protocol):
    def recognize(self, image_bytes: bytes) -> list[dict]: ...


@dataclass(frozen=True)
class Rect:
    """Top-left image pixels, right/bottom excluded; never auto-scaled."""

    box: tuple[int, int, int, int]

    def __post_init__(self):
        if len(self.box) != 4 or any(type(v) is not int for v in self.box):
            raise ValueError("ROI must contain four integers")
        x, y, r, b = self.box
        if x < 0 or y < 0 or r <= x or b <= y:
            raise ValueError("ROI must have positive area inside the image")

    def contains(self, other: Rect) -> bool:
        x, y, r, b = self.box
        a, c, d, e = other.box
        return x <= a and y <= c and d <= r and e <= b

    def intersects(self, other: Rect) -> bool:
        x, y, r, b = self.box
        a, c, d, e = other.box
        return x < d and a < r and y < e and c < b


@dataclass(frozen=True)
class SlotLayout:
    slot: int
    card: Rect
    name_zone: Rect
    price: Rect
    interior: Rect


@dataclass(frozen=True)
class ShopLayout:
    layout_id: str
    image_size: tuple[int, int]
    slots: tuple[SlotLayout, ...]
    scales: tuple[int, ...] = (1, 2)
    min_confidence: float = 0.5
    empty_std_max: float = 8.0
    empty_mean_max: float = 40.0
    occlusion_terms: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.layout_id or len(self.image_size) != 2 or any(
            type(v) is not int or v <= 0 for v in self.image_size
        ):
            raise ValueError("An explicit layout ID and image size are required")
        if tuple(s.slot for s in self.slots) != (1, 2, 3, 4, 5):
            raise ValueError("Exactly five ordered slots are required")
        if len(self.scales) < 2 or len(set(self.scales)) != len(self.scales) or any(
            type(v) is not int or not 1 <= v <= 4 for v in self.scales
        ):
            raise ValueError("At least two distinct integer scales in 1..4 are required")
        if not math.isfinite(self.min_confidence) or not 0 <= self.min_confidence <= 1:
            raise ValueError("Invalid OCR confidence threshold")
        if any(not math.isfinite(v) or v < 0 for v in (self.empty_std_max, self.empty_mean_max)):
            raise ValueError("Invalid empty-slot heuristic threshold")
        canvas = Rect((0, 0, *self.image_size))
        for i, slot in enumerate(self.slots):
            if not canvas.contains(slot.card) or any(not slot.card.contains(r) for r in (
                slot.name_zone, slot.price, slot.interior
            )):
                raise ValueError("All ROIs must fit their card and exact image size")
            if slot.name_zone.intersects(slot.price):
                raise ValueError("Name and price ROIs must not overlap")
            if any(slot.card.intersects(s.card) for s in self.slots[:i]):
                raise ValueError("Shop slots must not overlap")
        if any(not isinstance(t, str) or not t.strip() for t in self.occlusion_terms):
            raise ValueError("Occlusion terms must be nonempty strings")


@dataclass(frozen=True)
class LexiconEntry:
    key: str
    name: str
    aliases: tuple[str, ...] = ()
    reference_cost: int | None = None

    def __post_init__(self):
        if not self.key or not self.name.strip():
            raise ValueError("Vocabulary entries require a key and name")
        if any(not isinstance(a, str) or not a.strip() for a in self.aliases):
            raise ValueError("Invalid vocabulary alias")
        if self.reference_cost is not None and (
            type(self.reference_cost) is not int or self.reference_cost < 0
        ):
            raise ValueError("Invalid unverified reference cost")


@dataclass(frozen=True)
class FieldCandidate:
    status: str
    candidates: tuple[Any, ...]
    reasons: tuple[str, ...]
    # Each scale remains visible, including an empty list when OCR loses text.
    variants: tuple[dict, ...]


@dataclass(frozen=True)
class SlotResult:
    slot: int
    status: str
    name: FieldCandidate
    price: FieldCandidate
    presence: str
    occlusion: str
    reasons: tuple[str, ...]
    reference_cost_check: str
    name_options: tuple[dict, ...] = ()
    visibility: str = "unverified"
    identity_verified: bool = False
    font_legibility: str = "unverified"


@dataclass(frozen=True)
class ShopProbeResult:
    frame_ref: str
    image_size: tuple[int, int] | None
    layout_id: str
    slots: tuple[SlotResult, ...]
    elapsed_ms: float
    ocr_requests: int
    issues: tuple[str, ...] = ()
    _private: dict = field(default_factory=dict, repr=False, compare=False)

    def summary(self) -> dict:
        """Anonymous byte reference + candidates; no paths, ROIs or OCR text."""
        return {
            "format": "shop-probe-v1", "usage": "offline_development_only",
            "frame_ref": self.frame_ref, "source_sha256": self.frame_ref.removeprefix("sha256:"),
            "image_size": self.image_size,
            "layout_id": self.layout_id, "slots": [asdict(s) for s in self.slots],
            "elapsed_ms": round(self.elapsed_ms, 3), "ocr_requests": self.ocr_requests,
            "issues": list(self.issues), "accuracy": None,
            "human_annotations": 0, "independent_reviews": 0,
            "device_capture_time": None, "actionable": False,
        }

    def private_details(self) -> dict:
        """Explicit opt-in local diagnostics, including real OCR and ROIs."""
        return {**self.summary(), "private_diagnostics": self._private}


def _normalize(text: str) -> str:
    # Whitespace/NFKC only. No fuzzy correction or substring name matching.
    return "".join(unicodedata.normalize("NFKC", text).split())


def _field(votes: list[dict]) -> FieldCandidate:
    all_candidates = tuple(sorted({c for v in votes for c in v["candidates"]}, key=str))
    reasons = {r for v in votes for r in v["reasons"]}
    if len(all_candidates) > 1:
        status = "conflict"
        reasons.add("candidate_disagreement")
    elif all_candidates and all(
        len(v["eligible"]) == 1 and not v["reasons"] for v in votes
    ):
        status = "candidate"
    else:
        status = "unknown"
        if all_candidates:
            reasons.add("candidate_missing_or_rejected_in_variant")
        else:
            reasons.add("no_candidate")
    return FieldCandidate(status, all_candidates, tuple(sorted(reasons)), tuple(votes))


def _unknown(slot: int, reason: str) -> SlotResult:
    f = FieldCandidate("unknown", (), (reason,), ())
    return SlotResult(slot, "unknown", f, f, "unknown", "unknown", (reason,), "not_comparable")


def _boxes(raw: Any, size: tuple[int, int]) -> list[dict]:
    """Reject malformed OCR atomically, preserving no usable partial answer."""
    if not isinstance(raw, list):
        raise ValueError("invalid_ocr_response")
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            raise ValueError("invalid_ocr_response")
        values = [item.get(k) for k in ("x", "y", "width", "height")]
        if any(type(v) is not int for v in values):
            raise ValueError("invalid_ocr_response")
        x, y, w, h = values
        c = item.get("confidence")
        if (not isinstance(c, (float, int)) or isinstance(c, bool) or not math.isfinite(c)
            or not 0 <= c <= 1 or x < 0 or y < 0 or w <= 0 or h <= 0
            or x + w > size[0] or y + h > size[1]):
            raise ValueError("invalid_ocr_response")
    return raw


def _vote(raw: list[dict], *, scale: int, crop: Rect, zone: Rect,
          lexicon: dict[str, set[str]] | None, min_confidence: float,
          error: str | None) -> dict:
    candidates, eligible, reasons = set(), set(), set()
    if error:
        reasons.add(error)
    seen = False
    for b in raw:
        # Compare exact scaled pixels without rounding boxes into another zone.
        x, y, r, bottom = zone.box
        a = b["x"] + crop.box[0] * scale
        c = b["y"] + crop.box[1] * scale
        d, e = a + b["width"], c + b["height"]
        intersects = a < r * scale and x * scale < d and c < bottom * scale and y * scale < e
        if not intersects:
            continue
        seen = True
        if not (x * scale <= a and y * scale <= c and d <= r * scale and e <= bottom * scale):
            reasons.add("text_crosses_roi_boundary")
            continue
        text = _normalize(b["text"])
        if lexicon is not None:
            matches = lexicon.get(text, set())
        else:
            # A whole numeric token; never pluck digits out of a tooltip.
            matches = {int(text)} if re.fullmatch(r"(?:0|[1-9][0-9]{0,2})", text) else set()
        candidates.update(matches)
        edge = (b["x"] == 0 or b["y"] == 0 or
                b["x"] + b["width"] == (crop.box[2] - crop.box[0]) * scale or
                b["y"] + b["height"] == (crop.box[3] - crop.box[1]) * scale)
        if not matches:
            reasons.add("outside_vocabulary" if lexicon is not None else "invalid_price_token")
        if edge:
            reasons.add("text_touches_crop_edge")
        if b["confidence"] < min_confidence:
            reasons.add("low_ocr_confidence")
        elif not edge:
            eligible.update(matches)
    if not seen:
        reasons.add("no_text_in_roi")
    return {"scale": scale, "candidates": tuple(sorted(candidates, key=str)),
            "eligible": tuple(sorted(eligible, key=str)), "reasons": tuple(sorted(reasons))}


def probe_shop(image_bytes: bytes, *, layout: ShopLayout,
               lexicon: Sequence[LexiconEntry], ocr: OCR,
               expected_sha256: str | None = None) -> ShopProbeResult:
    """Read an image once, bind its exact bytes, and always return slots 1..5.

    Bad image/hash/geometry returns five unknowns and performs zero OCR. Layout
    and vocabulary programming errors raise ValueError. OCR failures become
    explicit unknowns; failed/late worker responses are the backend's concern.
    """
    started = time.perf_counter_ns()
    digest = hashlib.sha256(image_bytes).hexdigest()
    frame_ref = "sha256:" + digest
    calls, size, private = 0, None, {"slots": [], "layout": asdict(layout)}

    def finish(slots, issues=()):
        return ShopProbeResult(frame_ref, size, layout.layout_id, tuple(slots),
                               (time.perf_counter_ns() - started) / 1e6, calls,
                               tuple(issues), private)

    if expected_sha256 is not None and digest != expected_sha256:
        return finish([_unknown(i, "source_hash_mismatch") for i in range(1, 6)],
                      ("source_hash_mismatch",))
    entries = {e.key: e for e in lexicon}
    if len(entries) != len(lexicon):
        raise ValueError("Vocabulary keys must be unique; do not merge name collisions")
    lookup: dict[str, set[str]] = {}
    for e in lexicon:
        for alias in (e.name, *e.aliases):
            lookup.setdefault(_normalize(alias), set()).add(e.key)
    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            size = image.size
            if size != layout.image_size:
                reason = "image_geometry_mismatch"
            elif image.getexif().get(274, 1) != 1:
                reason = "unapplied_image_orientation"
            elif getattr(image, "n_frames", 1) != 1:
                reason = "multiframe_image_unsupported"
            else:
                reason = None
                image.load()
                decoded = image.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError):
        reason = "image_decode_failed"
    if reason:
        return finish([_unknown(i, reason) for i in range(1, 6)], (reason,))

    slots = []
    for slot in layout.slots:
        name_votes, price_votes, body_text, details = [], [], [], []
        for scale in layout.scales:
            for kind, crop in (("card", slot.card), ("price", slot.price)):
                region = decoded.crop(crop.box)
                region = region.resize((region.width * scale, region.height * scale), Image.Resampling.LANCZOS)
                data = io.BytesIO()
                region.save(data, format="PNG")
                error = None
                t = time.perf_counter_ns()
                calls += 1
                try:
                    raw = _boxes(ocr.recognize(data.getvalue()), region.size)
                except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
                    raw = []
                    error = "invalid_ocr_response" if str(exc) == "invalid_ocr_response" else "ocr_error"
                details.append({"kind": kind, "scale": scale, "raw": raw,
                                "error": error, "elapsed_ms": (time.perf_counter_ns() - t) / 1e6})
                if kind == "card":
                    name_votes.append(_vote(raw, scale=scale, crop=crop, zone=slot.name_zone,
                                            lexicon=lookup, min_confidence=layout.min_confidence, error=error))
                    for b in raw:
                        # Only card body text supplies overlay clues.
                        if crop.box[1] * scale + b["y"] + b["height"] <= slot.name_zone.box[1] * scale:
                            if b["confidence"] >= layout.min_confidence:
                                body_text.append(_normalize(b["text"]))
                else:
                    price_votes.append(_vote(raw, scale=scale, crop=crop, zone=crop,
                                             lexicon=None, min_confidence=layout.min_confidence, error=error))
        name, price = _field(name_votes), _field(price_votes)
        stats = ImageStat.Stat(decoded.crop(slot.interior.box))
        flat_dark = max(stats.stddev) <= layout.empty_std_max and max(stats.mean) <= layout.empty_mean_max
        all_blank = all(not d["raw"] and not d["error"] for d in details)
        suspected_overlay = any(_normalize(t) in b for t in layout.occlusion_terms for b in body_text)
        presence = "empty_candidate" if flat_dark and all_blank else "content_candidate"
        reasons = set()
        check = "not_comparable"
        if len(name.candidates) == len(price.candidates) == 1:
            ref_cost = entries[name.candidates[0]].reference_cost
            if ref_cost is not None:
                check = "agrees_with_unverified_reference" if ref_cost == price.candidates[0] else "differs_from_unverified_reference"
                if ref_cost != price.candidates[0]:
                    reasons.add("unverified_reference_cost_disagreement")
        if suspected_overlay:
            status = "occluded_candidate"
            reasons.add("body_overlay_text_cue")
        elif name.status == "conflict" or price.status == "conflict":
            status = "conflict"
        elif flat_dark and not all_blank:
            status = "unknown"
            presence = "unknown"
            reasons.add("flat_dark_but_text_or_ocr_failure")
        elif presence == "empty_candidate":
            status = "empty_candidate"
            reasons.add("dark_low_variance_and_all_ocr_blank")
        elif name.status == price.status == "candidate" and not reasons:
            status = "candidate"
        else:
            status = "unknown"
        if not all_blank and any(d["error"] for d in details):
            reasons.add("ocr_failure")
        if status in ("unknown", "conflict"):
            reasons.update(name.reasons + price.reasons)
        slots.append(SlotResult(slot.slot, status, name, price, presence,
                                "suspected" if suspected_overlay else "not_detected",
                                tuple(sorted(reasons)), check,
                                name_options=tuple({"key": k, "name": entries[k].name}
                                                   for k in name.candidates)))
        private["slots"].append({"slot": slot.slot, "ocr": details,
                                 "interior_mean": stats.mean, "interior_stddev": stats.stddev})
    # Keys disambiguate vocabulary aliases, while display names remain explicit.
    private["vocabulary"] = [asdict(e) for e in lexicon]
    private["ocr_score_calibrated"] = False
    return finish(slots)
