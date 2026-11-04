"""Conservative shop display-text candidates, separate from identity lookup.

This module deliberately does not accept a name vocabulary.  A candidate is
only text displayed inside one configured name zone; it is never a champion
identity, price, buyability, or trait binding.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

from PIL import Image

from .probe import OCR, Rect, ShopLayout, _boxes, _normalize


CANDIDATE = "candidate"
UNKNOWN = "unknown"
SANITIZATION_EVIDENCE_FORMAT = "shop-display-text-sanitization-evidence/v1"


@dataclass(frozen=True)
class _ViewIdentity:
    """Internal identity for one OCR view; source authenticity is not implied."""

    frame_sha256: str
    crop: tuple[int, int, int, int]
    zone: tuple[int, int, int, int]
    scale: int
    slot: int

    def valid(self) -> bool:
        return (isinstance(self.frame_sha256, str) and len(self.frame_sha256) == 64
                and all(ch in "0123456789abcdef" for ch in self.frame_sha256)
                and all(isinstance(value, int) and not isinstance(value, bool)
                        for value in (*self.crop, *self.zone))
                and type(self.scale) is int and self.scale in (1, 2)
                and type(self.slot) is int and 1 <= self.slot <= 5)


@dataclass(frozen=True)
class _DisplayTextView:
    identity: _ViewIdentity | None
    status: str
    normalized_text: str | None
    reason_codes: tuple[str, ...]
    raw_ocr_score: float | int | None
    raw_text_safety: str = "unknown"
    contains_control_or_format: bool | None = None


@dataclass(frozen=True)
class ShopDisplayTextSlot:
    """One unclassified display-text outcome for a fixed shop slot."""

    slot: int
    status: str
    display_text_normalized_candidate: str | None
    reasons: tuple[str, ...]
    variants: tuple[dict, ...]
    display_kind: str = "unclassified"
    identity_status: str = "unknown"
    price_status: str = "unknown"
    buyability: str = "unknown"
    trait_binding: str = "unknown"


@dataclass(frozen=True)
class ShopDisplayTextProbeResult:
    """Byte-bound machine candidates; the digest does not authenticate origin."""

    frame_ref: str
    image_size: tuple[int, int] | None
    layout_id: str
    slots: tuple[ShopDisplayTextSlot, ...]
    elapsed_ms: float
    ocr_requests: int
    issues: tuple[str, ...] = ()
    _sanitization: tuple[tuple[_DisplayTextView, ...], ...] = ()

    def summary(self) -> dict:
        """Return candidates without image, paths, ROIs, or raw OCR boxes."""
        return {
            "format": "shop-display-text-probe-v1",
            "usage": "offline_development_only",
            "frame_ref": self.frame_ref,
            "source_sha256": self.frame_ref.removeprefix("sha256:"),
            "source_authentication": "not_authenticated",
            "image_size": self.image_size,
            "layout_id": self.layout_id,
            "slots": [asdict(slot) for slot in self.slots],
            "elapsed_ms": round(self.elapsed_ms, 3),
            "ocr_requests": self.ocr_requests,
            "issues": list(self.issues),
            "accuracy": None,
            "human_annotations": 0,
            "independent_reviews": 0,
            "actionable": False,
        }

    def sanitization_evidence_v1(self) -> dict:
        """Return a separately-versioned raw-text safety witness.

        This never returns OCR text, images, boxes, paths, or private timing.
        Its hashes bind it to the exact v1 summary supplied to a downstream
        consumer; they are not source authentication.
        """
        summary = self.summary()
        by_slot = {slot.slot: views for slot, views in zip(self.slots, self._sanitization)}
        slots = []
        for slot in self.slots:
            views = by_slot.get(slot.slot, ())
            scales = {view.identity.scale: view for view in views if view.identity is not None}
            variants = []
            for scale in (1, 2):
                view = scales.get(scale)
                variants.append({
                    "scale": scale,
                    "raw_text_safety": view.raw_text_safety if view is not None else "unknown",
                    "contains_control_or_format": (view.contains_control_or_format
                                                   if view is not None else None),
                })
            slots.append({"slot": slot.slot,
                          "display_slot_binding_sha256": display_text_slot_binding_sha256(summary, slot.slot),
                          "variants": variants})
        return {
            "format": SANITIZATION_EVIDENCE_FORMAT,
            "usage": "offline_development_only",
            "display_summary_format": summary["format"],
            "display_summary_binding_sha256": display_text_binding_sha256(summary),
            "frame_ref": self.frame_ref,
            "source_sha256": self.frame_ref.removeprefix("sha256:"),
            "layout_id": self.layout_id,
            "source_authentication": "not_authenticated",
            "normalization_policy": "NFKC_then_remove_whitespace_after_rejecting_Cc_Cf_Cs",
            "slots": slots,
            "accuracy": None,
            "human_annotations": 0,
            "independent_reviews": 0,
            "actionable": False,
        }


def _canonical_sha256(value: Any) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("display_summary_not_strict_json") from exc
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def display_text_binding_sha256(summary: Any) -> str:
    """Hash the exact supplied v1 display summary for cross-object binding.

    The caller must separately validate the v1 display contract.  This helper
    intentionally establishes only data equality, never capture provenance.
    """
    if type(summary) is not dict or summary.get("format") != "shop-display-text-probe-v1":
        raise ValueError("display_summary_format_invalid")
    return _canonical_sha256(summary)


def display_text_slot_binding_sha256(summary: Any, slot: Any) -> str:
    """Bind one declared slot to one exact supplied v1 display summary."""
    if type(slot) is not int or not 1 <= slot <= 5:
        raise ValueError("display_slot_invalid")
    binding = display_text_binding_sha256(summary)
    rows = summary.get("slots")
    if (type(rows) not in (list, tuple) or sum(type(row) is dict and row.get("slot") == slot
                                               for row in rows) != 1):
        raise ValueError("display_slot_not_bound")
    return _canonical_sha256({"display_summary_binding_sha256": binding,
                              "slot": slot})


def _unknown(slot: int, reason: str) -> ShopDisplayTextSlot:
    return ShopDisplayTextSlot(slot, UNKNOWN, None, (reason,), ())


def _control_or_format_free(text: str) -> bool:
    """Keep normal whitespace normalization, but reject Cc/Cf/Cs before it."""
    return not any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in text)


def _variant(raw: list[dict], *, identity: _ViewIdentity,
             min_confidence: float) -> _DisplayTextView:
    """Require exactly one fully-contained, non-edge text box in this zone."""
    crop_left, crop_top, crop_right, crop_bottom = identity.crop
    zone_left, zone_top, zone_right, zone_bottom = identity.zone
    scale = identity.scale
    selected: list[dict] = []
    for box in raw:
        left = crop_left * scale + box["x"]
        top = crop_top * scale + box["y"]
        right, bottom = left + box["width"], top + box["height"]
        intersects = (left < zone_right * scale and zone_left * scale < right
                      and top < zone_bottom * scale and zone_top * scale < bottom)
        if not intersects:
            continue
        if not (zone_left * scale <= left and zone_top * scale <= top
                and right <= zone_right * scale and bottom <= zone_bottom * scale):
            return _DisplayTextView(identity, UNKNOWN, None,
                                    ("text_crosses_roi_boundary",), None)
        selected.append(box)
    if len(selected) != 1:
        return _DisplayTextView(identity, UNKNOWN, None,
                                ("no_single_text_in_roi",), None)
    box = selected[0]
    left = crop_left * scale + box["x"]
    top = crop_top * scale + box["y"]
    right, bottom = left + box["width"], top + box["height"]
    if not _control_or_format_free(box["text"]):
        return _DisplayTextView(identity, UNKNOWN, None,
                                ("raw_text_control_or_format_character",), box["confidence"],
                                "unsafe_control_or_format", True)
    text = _normalize(box["text"])
    if box["confidence"] < min_confidence:
        return _DisplayTextView(identity, UNKNOWN, None, ("low_ocr_confidence",),
                                box["confidence"])
    if not text:
        return _DisplayTextView(identity, UNKNOWN, None, ("invalid_display_text",), None,
                                "safe", False)
    at_crop_edge = (box["x"] == 0 or box["y"] == 0
                    or box["x"] + box["width"] == (crop_right - crop_left) * scale
                    or box["y"] + box["height"] == (crop_bottom - crop_top) * scale)
    at_zone_edge = (left == zone_left * scale or top == zone_top * scale
                    or right == zone_right * scale or bottom == zone_bottom * scale)
    if at_zone_edge:
        return _DisplayTextView(identity, UNKNOWN, None, ("text_touches_roi_edge",),
                                box["confidence"], "safe", False)
    if at_crop_edge:
        return _DisplayTextView(identity, UNKNOWN, None, ("text_touches_crop_edge",),
                                box["confidence"], "safe", False)
    return _DisplayTextView(identity, CANDIDATE, text, (), box["confidence"], "safe", False)


def _view_record(view: _DisplayTextView) -> dict:
    return {
        "scale": None if view.identity is None else view.identity.scale,
        "status": view.status,
        "reason_codes": list(view.reason_codes),
        "raw_ocr_score": view.raw_ocr_score,
    }


def _reconcile(views: list[_DisplayTextView]) -> ShopDisplayTextSlot:
    """Reconcile exactly one 1x and one 2x view of the same slot and zone."""
    identities = [view.identity for view in views]
    contract = (len(views) == 2 and all(identity is not None and identity.valid()
                                         for identity in identities))
    if contract:
        typed = [identity for identity in identities if identity is not None]
        contract = (len({identity.scale for identity in typed}) == 2
                    and {identity.scale for identity in typed} == {1, 2}
                    and len({identity.frame_sha256 for identity in typed}) == 1
                    and len({identity.crop for identity in typed}) == 1
                    and len({identity.zone for identity in typed}) == 1
                    and len({identity.slot for identity in typed}) == 1)
    slot = identities[0].slot if identities and identities[0] is not None else 0
    records = tuple(_view_record(view) for view in views)
    if not contract:
        return ShopDisplayTextSlot(slot, UNKNOWN, None, ("invalid_related_view_contract",), records)
    if any(view.status != CANDIDATE for view in views):
        reasons = sorted({reason for view in views for reason in view.reason_codes})
        return ShopDisplayTextSlot(slot, UNKNOWN, None,
                                   tuple(reasons or ["variant_incomplete"]), records)
    candidates = {view.normalized_text for view in views}
    if len(candidates) != 1:
        return ShopDisplayTextSlot(slot, UNKNOWN, None, ("candidate_disagreement",), records)
    return ShopDisplayTextSlot(slot, CANDIDATE, next(iter(candidates)), (), records)


def probe_shop_display_text(image_bytes: bytes, *, layout: ShopLayout, ocr: OCR,
                            expected_sha256: str | None = None) -> ShopDisplayTextProbeResult:
    """Read only displayed name-zone text; do not infer game identities or actions.

    `frame_ref` proves only the bytes passed to this function.  The caller's
    claimed source, timing, scene, and authenticity remain unverified.
    """
    started = time.perf_counter_ns()
    digest = hashlib.sha256(image_bytes).hexdigest()
    frame_ref = "sha256:" + digest
    calls, size = 0, None

    def finish(slots: list[ShopDisplayTextSlot], issues: tuple[str, ...] = (), sanitization=()):
        return ShopDisplayTextProbeResult(frame_ref, size, layout.layout_id, tuple(slots),
                                           (time.perf_counter_ns() - started) / 1e6, calls, issues,
                                           tuple(sanitization))

    if expected_sha256 is not None and digest != expected_sha256:
        return finish([_unknown(slot, "source_hash_mismatch") for slot in range(1, 6)],
                      ("source_hash_mismatch",))
    if len(layout.scales) != 2 or set(layout.scales) != {1, 2}:
        return finish([_unknown(slot, "display_text_requires_exact_scales_1_and_2")
                       for slot in range(1, 6)], ("display_text_requires_exact_scales_1_and_2",))
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
                image.load()
                decoded, reason = image.convert("RGB"), None
    except (OSError, ValueError, Image.DecompressionBombError):
        reason = "image_decode_failed"
    if reason:
        return finish([_unknown(slot, reason) for slot in range(1, 6)], (reason,))

    slots: list[ShopDisplayTextSlot] = []
    sanitization: list[tuple[_DisplayTextView, ...]] = []
    for slot_layout in layout.slots:
        crop, zone = slot_layout.card, slot_layout.name_zone
        views: list[_DisplayTextView] = []
        for scale in layout.scales:
            identity = _ViewIdentity(digest, crop.box, zone.box, scale, slot_layout.slot)
            region = decoded.crop(crop.box).resize(
                ((crop.box[2] - crop.box[0]) * scale,
                 (crop.box[3] - crop.box[1]) * scale),
                Image.Resampling.LANCZOS,
            )
            calls += 1
            try:
                response = ocr.recognize(_png(region))
            except Exception:
                # OCR is an external backend boundary. Deliberately do not
                # catch BaseException: interrupts and interpreter exits pass through.
                views.append(_DisplayTextView(identity, UNKNOWN, None, ("ocr_error",), None))
                continue
            try:
                raw = _boxes(response, region.size)
            except (OverflowError, TypeError, ValueError):
                views.append(_DisplayTextView(identity, UNKNOWN, None,
                                              ("invalid_ocr_response",), None))
                continue
            views.append(_variant(raw, identity=identity, min_confidence=layout.min_confidence))
        slots.append(_reconcile(views))
        sanitization.append(tuple(views))
    return finish(slots, sanitization=sanitization)


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()
