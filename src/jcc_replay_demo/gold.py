"""A candidate veto from pixels, independent of the OCR transcription/score.

This is a development check for upright, separated, light-on-dark 6/9 glyphs.
Any boundary foreground is ambiguous and vetoes the candidate. Its absence
does not prove that the ROI contains a complete number.
It neither transcribes other digits nor repairs an OCR answer. Consistent
topology is not a calibrated recognizer, human truth, or semantic evidence.
"""

from __future__ import annotations

import hashlib
import io
import math

from PIL import Image


METHOD = "six-nine-loop-topology-v2"
THRESHOLDS = (128, 160, 192, 224)
MAX_CROP_PIXELS = 128_000


def needs_glyph_check(value) -> bool:
    return type(value) is int and any(c in "69" for c in str(value))


def _components(points):
    remaining = set(points)
    result = []
    while remaining:
        start = remaining.pop()
        pending, component = [start], {start}
        while pending:
            x, y = pending.pop()
            for neighbour in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if neighbour in remaining:
                    remaining.remove(neighbour)
                    component.add(neighbour)
                    pending.append(neighbour)
        result.append(component)
    return result


def _loop_position(holes):
    if not isinstance(holes, list) or len(holes) != 1:
        return "unresolved"
    center = holes[0]["center_y"]
    if isinstance(center, bool) or not isinstance(center, (int, float)) or not math.isfinite(center):
        return "unresolved"
    if 0.15 <= center <= 0.40:
        return "upper"
    if 0.60 <= center <= 0.85:
        return "lower"
    return "unresolved"


def _measurement(crop, threshold):
    width, height = crop.size
    pixels = crop.load()
    foreground = ((x, y) for y in range(height) for x in range(width)
                  if pixels[x, y] >= threshold)
    glyphs = []
    boundary_foreground_pixels = 0
    for component in _components(foreground):
        left, right = min(x for x, _ in component), max(x for x, _ in component)
        top, bottom = min(y for _, y in component), max(y for _, y in component)
        # A clipped stroke and an icon cannot be distinguished by touching
        # the ROI edge. Retain this evidence even for a single-pixel fragment;
        # do not let a complete interior glyph hide it from the veto below.
        if left == 0 or top == 0 or right == width - 1 or bottom == height - 1:
            boundary_foreground_pixels += sum(
                x == 0 or y == 0 or x == width - 1 or y == height - 1
                for x, y in component)
            continue
        if len(component) < 4:
            continue
        holes = []
        background = ((x, y) for y in range(top, bottom + 1)
                      for x in range(left, right + 1) if (x, y) not in component)
        area = (right - left + 1) * (bottom - top + 1)
        for hole in _components(background):
            if (any(x in (left, right) or y in (top, bottom) for x, y in hole)
                    or len(hole) < max(4, math.ceil(area * 0.01))):
                continue
            center_y = (sum(y for _, y in hole) / len(hole) - top) / max(1, bottom - top)
            holes.append({"pixels": len(hole), "center_y": center_y})
        glyphs.append({"box": [left, top, right + 1, bottom + 1],
                       "foreground_pixels": len(component), "holes": holes,
                       "loop_position": _loop_position(holes)})
    glyphs.sort(key=lambda g: g["box"][0])
    return {"threshold": threshold, "components": glyphs,
            "boundary_foreground_pixels": boundary_foreground_pixels}


def _topology_status(value: int, measurements: list) -> str:
    boundary_counts = [m.get("boundary_foreground_pixels") for m in measurements]
    if any(type(n) is not int or n < 0 for n in boundary_counts):
        return "gold_glyph_inconclusive"
    if any(boundary_counts):
        return "gold_roi_boundary_foreground"
    text = str(value)
    complete = True
    for measurement in measurements:
        glyphs = measurement["components"]
        if (len(glyphs) != len(text)
                or any(a["box"][2] > b["box"][0] for a, b in zip(glyphs, glyphs[1:]))):
            complete = False
            continue
        for digit, glyph in zip(text, glyphs):
            if digit not in "69":
                continue
            observed = _loop_position(glyph["holes"])
            if glyph["loop_position"] != observed:
                return "gold_glyph_inconclusive"
            if observed == "unresolved":
                complete = False
            elif observed != ("lower" if digit == "6" else "upper"):
                return "gold_glyph_disagreement"
    return "consistent" if complete else "gold_glyph_inconclusive"


def check_gold_glyph(data: bytes, *, roi, frame_ref: str, proposed_value) -> dict:
    result = {"method": METHOD, "input_frame_ref": frame_ref,
              "input_source_sha256": hashlib.sha256(data).hexdigest(),
              "input_roi": list(roi), "checked_value": proposed_value,
              "required": needs_glyph_check(proposed_value),
              "status": "not_required", "reason": None, "measurements": []}
    if proposed_value is None:
        result.update(status="not_checked", reason="gold_numeric_unknown")
        return result
    if not result["required"]:
        return result
    result.update(status="unknown", reason="gold_glyph_inconclusive")
    try:
        if (len(roi) != 4 or any(type(n) is not int for n in roi)
                or not 0 < (roi[2] - roi[0]) * (roi[3] - roi[1]) <= MAX_CROP_PIXELS):
            return result
        with Image.open(io.BytesIO(data)) as image:
            if (not 0 <= roi[0] < roi[2] <= image.width
                    or not 0 <= roi[1] < roi[3] <= image.height):
                return result
            with image.crop(roi).convert("L") as crop:
                result["measurements"] = [_measurement(crop, t) for t in THRESHOLDS]
    except (OSError, ValueError, TypeError, Image.DecompressionBombError):
        return result
    outcome = _topology_status(proposed_value, result["measurements"])
    result.update(status="consistent" if outcome == "consistent" else "unknown",
                  reason=None if outcome == "consistent" else outcome)
    return result


def glyph_check_allows(frame: dict, value: int, roi) -> bool:
    """Consume only this exact value's bound, internally consistent check."""
    if type(value) is not int:
        return False
    required = needs_glyph_check(value)
    check = frame.get("gold_glyph_check")
    if (not isinstance(check, dict) or check.get("method") != METHOD
            or check.get("required") is not required
            or check.get("status") != ("consistent" if required else "not_required")
            or check.get("reason") is not None or type(check.get("checked_value")) is not int
            or check["checked_value"] != value
            or check.get("input_source_sha256") != frame.get("source_sha256")
            or check.get("input_frame_ref") != frame.get("frame_ref")
            or check.get("input_roi") != list(roi or ())):
        return False
    measurements = check.get("measurements")
    if not required:
        return measurements == []
    if (not isinstance(measurements, list) or len(measurements) != len(THRESHOLDS)
            or [m.get("threshold") for m in measurements if isinstance(m, dict)] != list(THRESHOLDS)):
        return False
    try:
        return _topology_status(value, measurements) == "consistent"
    except (KeyError, TypeError, ValueError, IndexError):
        return False


def structured_gold_check(check: dict) -> dict:
    """No pixels, OCR boxes, crop coordinates, or file paths in the adapter."""
    result = {k: check[k] for k in ("method", "input_frame_ref", "input_source_sha256",
                                   "checked_value", "required", "status", "reason")}
    result["loop_observations"] = [
        {"threshold": m["threshold"],
         "boundary_foreground_present": m["boundary_foreground_pixels"] > 0,
         "positions": [g["loop_position"] for g in m["components"]]}
        for m in check["measurements"]]
    return result
