"""Hash/ROI-bound capacity and bench appearance discovery on explicit stills.

Only the existing 17C reader may supply numeric capacity candidates. Template
similarity and foreground regions are appearance signals, not unit identities.
Caller observations gate each region but are never treated as human labels.
No device, previous-frame cache, network, game-state publishing or action API.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import time

import numpy as np
from PIL import Image, UnidentifiedImageError

from jcc_capacity_probe import FrameEvidence, ProbeLayout, probe_capacity
from jcc_capacity_probe.probe import IMPLEMENTATION_REVISION as CAPACITY_REVISION
from jcc_capacity_probe.probe import parse_capacity_ratio

METHOD = "open-slot-probe-v1"
REVISION = "offline-bound-appearance-v1"
MAX_BYTES = 25 * 1024 * 1024
MAX_PIXELS = 16_000_000
_ID = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("invalid_anonymous_identifier")
    return value


def digest(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("invalid_sha256")
    return value


def box(value, size):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(v) is not int for v in value)):
        raise ValueError("invalid_roi")
    l, t, r, b = value
    if not 0 <= l < r <= size[0] or not 0 <= t < b <= size[1]:
        raise ValueError("invalid_roi")
    return list(value)


def bounded(value, low, high):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high):
        raise ValueError("invalid_detector_parameter")
    return value


def decode(payload):
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_BYTES:
        raise ValueError("image_unreadable_or_too_large")
    try:
        with Image.open(io.BytesIO(payload)) as im:
            if im.format not in ("PNG", "JPEG") or getattr(im, "n_frames", 1) != 1:
                raise ValueError("unsupported_image")
            if math.prod(im.size) > MAX_PIXELS:
                raise ValueError("image_too_large")
            if im.getexif().get(274, 1) != 1:
                raise ValueError("unapplied_exif_rotation")
            im.load()
            return im.convert("RGB")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        raise ValueError("image_unreadable") from None


def _region_gate(payload, size, source_kind, region):
    """Bind every observation to its COMPLETE region, not a detected sub-box."""
    roi = box(region["roi"], size)
    evidence = FrameEvidence(**region["evidence"])
    sha = hashlib.sha256(payload).hexdigest()
    if (evidence.encoded_sha256 != sha or evidence.image_size != tuple(size)
            or evidence.layout_id != identifier(region["layout_id"])
            or evidence.source_kind != source_kind
            or evidence.rotation_applied_degrees != 0):
        return ["frame_evidence_mismatch"]
    if evidence.roi != tuple(roi):
        return ["frame_evidence_roi_mismatch"]
    method = "synthetic" if source_kind == "synthetic" else "agent_development_observation"
    if evidence.assessment_method != method:
        return ["evidence_authority_mismatch"]
    reasons = []
    if evidence.layout_matches is not True:
        reasons.append("layout_mismatch" if evidence.layout_matches is False else "layout_identity_unknown")
    if evidence.visibility != "clear_candidate":
        reasons.append("occluded" if evidence.visibility == "occluded" else "visibility_unknown")
    if evidence.animation != "not_observed_candidate":
        reasons.append("animation_observed" if evidence.animation == "observed" else "animation_unknown")
    return reasons


def capacity_candidates(payload, source_kind, trials, ocr):
    """All declared 17C trials must pass and agree; no best-result selection."""
    if CAPACITY_REVISION != "roi-bound-sanitized-variants-v2":
        raise ValueError("unsupported_capacity_reader_revision")
    if any(trial["layout"].get("rotation_applied_degrees", 0) != 0 for trial in trials):
        raise ValueError("unsupported_capacity_rotation_declaration")
    results, numbers = [], []
    for trial in trials:
        result = probe_capacity(
            payload, source_kind=source_kind, layout=ProbeLayout(**trial["layout"]),
            evidence=FrameEvidence(**trial["evidence"]), ocr=ocr,
            expected_sha256=trial["evidence"]["encoded_sha256"],
        ).to_dict()
        numbers.append((result["displayed_deployed"], result["capacity"], result["empty_slots"]))
        # Never copy 17C's conditional suggestion: this module is replay only.
        results.append({k: result[k] for k in (
            "source_frame", "layout_id", "input_roi", "status", "reason_codes", "confidence",
            "implementation_revision",
        )})
    accepted = bool(results) and all(r["status"] == "development_candidate" for r in results)
    reasons = list(dict.fromkeys(code for r in results for code in r["reason_codes"]))
    if not results:
        reasons.append("capacity_trials_missing")
    elif not accepted:
        reasons.insert(0, "strict_trial_incomplete")
    elif len(set(numbers)) != 1:
        accepted = False
        reasons.append("strict_trial_disagreement")
    if not accepted:
        # Even successful per-trial diagnostics must not leak rejected answers.
        for r in results:
            r["status"] = "unknown"
            for v in r["confidence"]["variants"]:
                v["status"] = "unknown"
                if v["reason"] is None:
                    v["reason"] = "discarded_due_to_unknown_result"
    n, c, empty = numbers[0] if accepted else (None, None, None)
    return {"status": "development_candidate" if accepted else "unknown",
            "displayed_deployed": n, "capacity": c, "empty_slots": empty,
            "reason_codes": reasons, "trials": results,
            "calibrated_probability": None, "reader_revision": CAPACITY_REVISION}


def diagnostic_ratio_scan(image, trials, ocr):
    """Raw/cyan diagnostic on the same crop. NO numeric answers leave here.

    A blocked crop can be inspected to diagnose OCR, but this path cannot
    populate capacity, certify visibility, or promote any candidate.
    """
    out = []
    for trial in trials:
        roi = box(trial["roi"], image.size)
        for transform in ("raw", "cyan"):
            pairs, variants = [], []
            for scale in (1, 2):
                t0 = time.perf_counter()
                crop = image.crop(roi)
                w, h = crop.size
                if w * h * scale * scale > MAX_PIXELS:
                    pairs.append(None)
                    variants.append({"scale": scale, "reason": "scaled_image_too_large"})
                    continue
                crop = crop.resize((w * scale, h * scale), Image.Resampling.NEAREST)
                if transform == "cyan":
                    r, g, b = np.asarray(crop, dtype=np.float32).transpose(2, 0, 1)
                    ink = (b > r + 25) & (g > r + 15) & (b > 100) & (g > 90)
                    crop = Image.fromarray(np.where(ink, 0, 255).astype("uint8")).convert("RGB")
                try:
                    encoded = io.BytesIO()
                    crop.save(encoded, format="PNG")
                    items = ocr.recognize(encoded.getvalue()) if ocr is not None else []
                    pair = parse_capacity_ratio(items[0].get("text")) if len(items) == 1 else None
                    count = len(items)
                    reason = None
                except Exception:
                    pair, count, reason = None, None, "ocr_failed"
                pairs.append(pair)
                variants.append({"scale": scale, "box_count": count,
                                 "complete_ratio_pattern_seen": pair is not None,
                                 "reason": reason, "seconds": time.perf_counter() - t0})
            out.append({"trial_id": identifier(trial["trial_id"]), "roi": roi,
                        "transform": transform, "diagnostic_only": True,
                        "same_complete_pattern_across_scales":
                            all(p is not None for p in pairs) and len(set(pairs)) == 1,
                        "variants": variants})
    return out


def _iou(a, b):
    overlap = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - overlap
    return overlap / area if area else 0


def bench_candidates(payload, image, source_kind, bench, references):
    """Foreground-region signals plus opt-in, private template comparisons.

    Region signals can span one object or share an object: never a unit count.
    A template is an Agent-selected visual exemplar, not an authenticated label.
    Non-detection NEVER establishes an empty bench or an absent chess piece.
    """
    started = time.perf_counter()
    result = {"status": "unknown", "object_presence": "unknown", "model_like_presence": "unknown",
              "resource_like_presence": "unknown", "deployable_unit": "unknown",
              "reason_codes": [], "regions": [], "matches": [], "reference_trials": [],
              "input_roi": None, "calibrated_probability": None}
    if bench is None:
        result["reason_codes"] = ["bench_unconfigured"]
        return result
    roi = box(bench["roi"], image.size)
    result["input_roi"] = roi
    reasons = _region_gate(payload, image.size, source_kind, bench)
    if reasons:
        result["reason_codes"] = reasons
        result["seconds"] = time.perf_counter() - started
        return result
    import cv2

    params = bench["parameters"]
    threshold = bounded(params["template_min_score"], 0.5, 0.99)
    dark_delta = bounded(params["foreground_dark_delta"], 10, 150)
    min_foreground = bounded(params["foreground_min_fraction"], 0.01, 0.8)
    min_edge = bounded(params["edge_min_fraction"], 0.001, 0.5)
    regions = bench["object_regions"]
    if not isinstance(regions, list) or not 1 <= len(regions) <= 32:
        raise ValueError("explicit_object_regions_required")
    for i, region in enumerate(regions):
        region = box(region, image.size)
        if not (roi[0] <= region[0] < region[2] <= roi[2]
                and roi[1] <= region[1] < region[3] <= roi[3]):
            raise ValueError("object_region_outside_bench")
        pixels = np.asarray(image.crop(region).resize((96, 96)), dtype=np.uint8)
        gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
        # A simple local contrast/texture proposal, not a learned classifier.
        dark_fraction = float(np.mean(gray.astype(float) < np.median(gray) - dark_delta))
        edge_fraction = float(np.mean(cv2.Canny(gray, 60, 140) > 0))
        present = dark_fraction >= min_foreground and edge_fraction >= min_edge
        result["regions"].append({"region_index": i, "roi": region,
                                  "status": "object_appearance_candidate" if present else "unknown",
                                  "dark_fraction": dark_fraction, "edge_fraction": edge_fraction})
    # Half-size template scan limits computation; no rescaling of game state.
    l, t, r, b = roi
    search = cv2.resize(np.asarray(image.crop(roi)), ((r - l) // 2, (b - t) // 2))
    matches = []
    if len(references) > 32:
        raise ValueError("too_many_references")
    for reference in references:
        meta = reference["metadata"]
        if meta["source_kind"] != source_kind:
            result["reference_trials"].append({"reference_id": meta["reference_id"], "reason": "reference_source_kind_mismatch"})
            continue
        template = cv2.resize(np.asarray(reference["crop"]),
                              (reference["crop"].width // 2, reference["crop"].height // 2))
        if (min(template.shape[:2]) < 4 or template.shape[0] > search.shape[0]
                or template.shape[1] > search.shape[1] or np.std(template) < 2):
            result["reference_trials"].append({"reference_id": meta["reference_id"], "reason": "template_geometry_or_texture_unusable"})
            continue
        scores = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
        best = float(np.max(scores))
        result["reference_trials"].append({"reference_id": meta["reference_id"],
                                           "max_similarity": best, "threshold": threshold})
        # Up to nine local maxima; matching the same sprite repeatedly is not
        # independent evidence and is not a piece count.
        for _ in range(9):
            _, score, _, at = cv2.minMaxLoc(scores)
            if not math.isfinite(score) or score < threshold:
                break
            x, y = at
            h, w = template.shape[:2]
            matched = [l + 2 * x, t + 2 * y, l + 2 * (x + w), t + 2 * (y + h)]
            # Require overlap with an explicitly identified bench body region.
            if any(_iou(matched, region) > 0.05 for region in regions):
                matches.append({"reference_id": meta["reference_id"], "class": meta["class"],
                                "roi": matched, "similarity": float(score),
                                "reference_binding": meta,
                                "self_frame_reference": meta["encoded_sha256"] == hashlib.sha256(payload).hexdigest()})
            scores[max(0, y - h // 2):y + h // 2 + 1,
                   max(0, x - w // 2):x + w // 2 + 1] = -1
    for candidate in sorted(matches, key=lambda x: x["similarity"], reverse=True):
        overlaps = [p for p in result["matches"] if _iou(p["roi"], candidate["roi"]) > 0.3]
        if overlaps:
            if any(p["class"] != candidate["class"] for p in overlaps):
                for p in overlaps:
                    p["class"] = "ambiguous_object"
            continue
        result["matches"].append(candidate)
    models = any(p["class"] == "model_appearance" for p in result["matches"])
    resources = any(p["class"] == "resource_appearance" for p in result["matches"])
    objects = bool(result["matches"]) or any(p["status"] != "unknown" for p in result["regions"])
    result.update(status="development_candidate" if objects else "unknown",
                  object_presence="appearance_candidate" if objects else "unknown",
                  model_like_presence="appearance_candidate" if models else "unknown",
                  resource_like_presence="appearance_candidate" if resources else "unknown",
                  reason_codes=["identity_ownership_and_legality_unverified"] if objects else ["no_supported_appearance_match_not_absence"],
                  seconds=time.perf_counter() - started)
    return result


def probe_frame(payload, frame, references=(), ocr=None):
    """One payload supplies ALL numeric, appearance and diagnostic paths."""
    started = time.perf_counter()
    source_kind = frame["source_kind"]
    if source_kind not in ("synthetic", "replay"):
        raise ValueError("offline_source_kind_required")
    sha = hashlib.sha256(payload).hexdigest()
    result = {"schema_version": METHOD, "implementation_revision": REVISION,
              "source_frame": {"anonymous_ref": "sha256:" + sha, "encoded_sha256": sha,
                               "source_kind": source_kind, "alias": identifier(frame["alias"]),
                               "viewing_group": identifier(frame["viewing_group"]),
                               "collection_activity": identifier(frame["collection_activity"]),
                               "image_size": None, "rotation_applied_degrees": 0},
              "status": "unknown", "capacity": None, "bench": None, "diagnostics": [],
              "reason_codes": [], "review_only_hint": "本帧输入未知；仅供复盘。",
              "context": {"subject": "unknown", "phase": "unknown", "deployable_bench_unit": "unknown",
                          "rules": "unknown", "temporal_stability": "unverified_single_frame",
                          "freshness": "historical_still_not_live", "human_labels_added": 0,
                          "independent_human_reviews_added": 0},
              "advice_eligible": False, "automatic_action": None, "accuracy": None}
    if sha != digest(frame["expected_sha256"]):
        result["reason_codes"] = ["source_hash_mismatch"]
        result["seconds"] = time.perf_counter() - started
        return result
    try:
        image = decode(payload)
    except ValueError as e:
        result["reason_codes"] = [str(e)]
        result["seconds"] = time.perf_counter() - started
        return result
    with image:
        result["source_frame"]["image_size"] = list(image.size)
        if list(image.size) != frame["image_size"]:
            result["reason_codes"] = ["image_geometry_mismatch"]
            result["seconds"] = time.perf_counter() - started
            return result
        trials = frame["capacity_trials"]
        diagnostics = frame.get("diagnostic_trials", [])
        if (not isinstance(trials, list) or len(trials) > 4
                or not isinstance(diagnostics, list) or len(diagnostics) > 4):
            raise ValueError("bounded_explicit_trials_required")
        result["capacity"] = capacity_candidates(payload, source_kind, trials, ocr)
        result["bench"] = bench_candidates(payload, image, source_kind, frame.get("bench"), references)
        # Explicit optional read-only diagnostic, never consumed by the merge.
        result["diagnostics"] = diagnostic_ratio_scan(image, diagnostics, ocr)
    capacity, bench = result["capacity"], result["bench"]
    if capacity["status"] == "development_candidate" and capacity["empty_slots"] > 0:
        result["status"] = "review_only_conditional_clue"
        result["review_only_hint"] = "仅供复盘：容量显示有候选空位；主体、阶段、规则及物体是否为合法棋子均未知。"
    elif capacity["status"] == "development_candidate":
        result["status"] = "review_only_observation"
        result["review_only_hint"] = "仅供复盘：容量显示为满额候选；备战席线索不构成上场条件。"
    elif bench["object_presence"] == "appearance_candidate":
        result["status"] = "review_only_observation"
        result["review_only_hint"] = "仅供复盘：备战席存在物体外观候选，容量未知；无法判断空位或上场条件。"
    else:
        result["review_only_hint"] = "仅供复盘：容量和备战席线索不足，保持未知。"
    result["reason_codes"] = ["subject_phase_rules_and_window_unverified"]
    result["seconds"] = time.perf_counter() - started
    return result
