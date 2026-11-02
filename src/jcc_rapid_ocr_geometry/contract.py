"""Fail-closed normalization for RapidOCR full detection output.

The existing ``jcc_rapid_ocr_candidate`` deliberately runs recognition only.
This independent adapter consumes a *serialized* full-detector result, keeps
the detector polygons, and rejects polygons that touch the declared crop
boundary.  It does not infer a full-frame ROI, field identity, visibility,
player ownership, game phase or an executable action.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from typing import Any


SCHEMA = "jcc-rapid-ocr-geometry-shadow-v1"


class GeometryError(ValueError):
    """Raised only for malformed caller contracts, never converted to a guess."""


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, Real):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _valid_dimensions(width: Any, height: Any) -> bool:
    return type(width) is int and type(height) is int and 1 <= width <= 4096 and 1 <= height <= 4096


def _polygon(value: Any, width: int, height: int) -> tuple[tuple[float, float], ...] | None:
    """Return a four-point polygon only when it is finite and within the crop."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    points: list[tuple[float, float]] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2 or not all(_finite(v) for v in point):
            return None
        x, y = float(point[0]), float(point[1])
        if not 0 <= x <= width - 1 or not 0 <= y <= height - 1:
            return None
        points.append((x, y))
    # An exactly duplicated point would not bound a text region.
    if len(set(points)) != 4:
        return None
    if _segments_intersect(points[0], points[1], points[2], points[3]):
        return None
    if _segments_intersect(points[1], points[2], points[3], points[0]):
        return None
    if any(abs(_orientation(points[index], points[(index + 1) % 4], points[(index + 2) % 4])) <= 1e-6
           for index in range(4)):
        # A positive polygon area can still hide a zero-angle vertex.  It is
        # not a four-corner text extent, including across the final/first
        # vertex boundary.
        return None
    # A fully collinear polygon is rejected with the signed area test.
    area = 0.0
    for current, following in zip(points, points[1:] + points[:1]):
        area += current[0] * following[1] - following[0] * current[1]
    if abs(area) <= 1e-6:
        return None
    return tuple(points)


def _orientation(first: tuple[float, float], second: tuple[float, float], third: tuple[float, float]) -> float:
    return ((second[0] - first[0]) * (third[1] - first[1])
            - (second[1] - first[1]) * (third[0] - first[0]))


def _on_segment(first: tuple[float, float], second: tuple[float, float], point: tuple[float, float]) -> bool:
    return (min(first[0], second[0]) <= point[0] <= max(first[0], second[0])
            and min(first[1], second[1]) <= point[1] <= max(first[1], second[1]))


def _segments_intersect(a: tuple[float, float], b: tuple[float, float],
                        c: tuple[float, float], d: tuple[float, float]) -> bool:
    """Reject crossing or collinear-overlapping opposite edges."""
    first, second, third, fourth = (_orientation(a, b, c), _orientation(a, b, d),
                                    _orientation(c, d, a), _orientation(c, d, b))
    if ((first > 0 > second or first < 0 < second)
            and (third > 0 > fourth or third < 0 < fourth)):
        return True
    return ((first == 0 and _on_segment(a, b, c))
            or (second == 0 and _on_segment(a, b, d))
            or (third == 0 and _on_segment(c, d, a))
            or (fourth == 0 and _on_segment(c, d, b)))


def _edge_touched(polygon: tuple[tuple[float, float], ...], width: int, height: int) -> bool:
    # Detector coordinates are pixels.  A point at 0 or at the final pixel is
    # a clipped/ambiguous extent, even when a library calls it a valid box.
    return any(x < 1 or y < 1 or x > width - 2 or y > height - 2 for x, y in polygon)


@dataclass(frozen=True)
class GeometryCandidate:
    """A machine observation; it remains ineligible for game/policy/action use."""

    status: str
    reason: str
    crop_width: int
    crop_height: int
    text: str | None
    score: float | None
    text_box: tuple[tuple[float, float], ...] | None
    raw_detection_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA,
            "status": self.status,
            "reason": self.reason,
            "evidence_kind": "caller_declared_detector_polygon_shadow_candidate",
            "backend_identity": "caller_declared_RapidOCROutput",
            "backend_identity_authenticated": False,
            "input_frame_binding_authenticated": False,
            "crop_geometry": {"width": self.crop_width, "height": self.crop_height},
            "text": self.text,
            "score": self.score,
            "text_box": [list(point) for point in self.text_box] if self.text_box else None,
            "raw_detection_count": self.raw_detection_count,
            "geometry_evidence": self.text_box is not None,
            "geometry_scope": "declared_crop_only",
            "human_annotation": False,
            "visibility": "unknown",
            "field_identity": "unknown",
            "eligible_for_geometry_gate": False,
            "eligible_for_policy": False,
            "eligible_for_execution": False,
            "accuracy": None,
        }


def _unknown(reason: str, width: int, height: int, count: int, *, text=None, score=None, box=None) -> GeometryCandidate:
    return GeometryCandidate("unknown", reason, width, height, text, score, box, count)


def normalize_full_result(*, crop_width: int, crop_height: int, raw: dict[str, Any], minimum_score: float = 0.5) -> GeometryCandidate:
    """Normalize serialized ``RapidOCROutput`` without choosing among lines.

    ``raw`` must contain one aligned entry in ``txts``, ``scores`` and
    ``boxes``.  More than one detector result is ambiguous for this narrow
    fixed-crop experiment; selecting the most confident line would hide that
    ambiguity.  Returned candidates are expressly shadow-only.
    """
    if not _valid_dimensions(crop_width, crop_height):
        raise GeometryError("invalid_crop_geometry")
    if not _finite(minimum_score) or not 0 <= minimum_score <= 1:
        raise GeometryError("invalid_minimum_score")
    if not isinstance(raw, dict) or raw.get("result_type") != "RapidOCROutput":
        return _unknown("invalid_backend_output", crop_width, crop_height, 0)
    texts, scores, boxes = raw.get("txts"), raw.get("scores"), raw.get("boxes")
    if any(not isinstance(values, (list, tuple)) for values in (texts, scores, boxes)):
        return _unknown("invalid_backend_output", crop_width, crop_height, 0)
    if not (len(texts) == len(scores) == len(boxes)):
        return _unknown("unaligned_backend_output", crop_width, crop_height, max(len(texts), len(scores), len(boxes)))
    count = len(texts)
    if count == 0:
        return _unknown("no_detection", crop_width, crop_height, 0)
    if count != 1:
        return _unknown("ambiguous_detection_count", crop_width, crop_height, count)
    text, score, raw_box = texts[0], scores[0], boxes[0]
    if not isinstance(text, str) or not text.strip() or len(text) > 8192:
        return _unknown("invalid_text", crop_width, crop_height, count)
    if not _finite(score) or not 0 <= score <= 1:
        return _unknown("invalid_score", crop_width, crop_height, count, text=text)
    box = _polygon(raw_box, crop_width, crop_height)
    if box is None:
        return _unknown("invalid_text_box", crop_width, crop_height, count, text=text, score=float(score))
    if _edge_touched(box, crop_width, crop_height):
        return _unknown("text_box_touches_crop_boundary", crop_width, crop_height, count,
                        text=text, score=float(score), box=box)
    if score < minimum_score:
        return _unknown("low_raw_score", crop_width, crop_height, count,
                        text=text, score=float(score), box=box)
    return GeometryCandidate("development_candidate", "interior_detector_box_available",
                             crop_width, crop_height, text, float(score), box, count)
