"""Narrow, read-only consumption of completed private shop probe results."""

import math
import re


SLOT_STATES = {"candidate", "empty_candidate", "occluded_candidate", "unknown", "conflict"}
FIELD_STATES = {"candidate", "unknown", "conflict"}


def _unknown_slot(slot, reason, *, status="unknown"):
    return {"slot": slot, "status": status, "name_status": "unknown", "names": [],
            "price_status": "unknown", "identity_verified": False, "reason_codes": [reason]}


def _codes(value):
    return (isinstance(value, list)
            and all(isinstance(r, str) and re.fullmatch(r"[a-z0-9_]{1,80}", r) for r in value)
            and len(set(value)) == len(value))


def _values(value, kind):
    if not isinstance(value, list):
        return None
    if kind == "name":
        valid = all(isinstance(v, str) and v and not any(ord(c) < 32 or ord(c) == 127 for c in v)
                    for v in value)
    else:
        valid = all(type(v) is int and 0 <= v <= 999 for v in value)
    return set(value) if valid and len(set(value)) == len(value) else None


def _field_scales(field, kind):
    """Check the producer's aggregation contract without promoting any status."""
    if (not isinstance(field, dict) or not isinstance(field.get("status"), str)
            or field["status"] not in FIELD_STATES or not _codes(field.get("reasons"))):
        return None
    candidates = _values(field.get("candidates"), kind)
    variants = field.get("variants")
    if candidates is None or not isinstance(variants, list) or not 2 <= len(variants) <= 4:
        return None
    scales, proposals, reasons, accepted = [], set(), set(), True
    for vote in variants:
        if (not isinstance(vote, dict) or set(vote) != {"scale", "candidates", "eligible", "reasons"}
                or type(vote["scale"]) is not int or not 1 <= vote["scale"] <= 4
                or not _codes(vote["reasons"])):
            return None
        values, eligible = _values(vote["candidates"], kind), _values(vote["eligible"], kind)
        if (values is None or eligible is None or not eligible <= values
                or not vote["reasons"] and (not values or values != eligible)
                or "no_text_in_roi" in vote["reasons"] and (values or eligible)
                or {"ocr_error", "invalid_ocr_response"}.intersection(vote["reasons"]) and (values or eligible)):
            return None
        scales.append(vote["scale"])
        proposals.update(values)
        reasons.update(vote["reasons"])
        accepted = accepted and len(eligible) == 1 and not vote["reasons"]
    if len(set(scales)) != len(scales):
        return None
    if len(proposals) > 1:
        status = "conflict"
        reasons.add("candidate_disagreement")
    elif proposals and accepted:
        status = "candidate"
    else:
        status = "unknown"
        reasons.add("candidate_missing_or_rejected_in_variant" if proposals else "no_candidate")
    if candidates != proposals or field["status"] != status or set(field["reasons"]) != reasons:
        return None
    return tuple(scales)


def _name_labels(name, options):
    if not isinstance(options, list) or len(options) != len(name["candidates"]):
        return None
    labels = {}
    for option in options:
        if not isinstance(option, dict) or not isinstance(option.get("key"), str):
            return None
        key, label = option["key"], option.get("name")
        if (key in labels or not isinstance(label, str) or not 0 < len(label) <= 80
                or label != label.strip() or any(ord(c) < 32 or ord(c) == 127 for c in label)):
            return None
        labels[key] = label
    if set(labels) != set(name["candidates"]):
        return None
    return [labels[k] for k in name["candidates"]]


def _slot_consistent(slot):
    """Validate the whole-slot state against fields and explicit image cues."""
    name, price = slot["name"], slot["price"]
    reasons, expected = set(slot["reasons"]), set()
    check = slot["reference_cost_check"]
    if slot["occlusion"] != "not_detected":
        return False
    if check != "not_comparable" and not (len(name["candidates"]) == len(price["candidates"]) == 1):
        return False
    if check == "differs_from_unverified_reference":
        expected.add("unverified_reference_cost_disagreement")
    if any({"ocr_error", "invalid_ocr_response"}.intersection(v["reasons"])
           for f in (name, price) for v in f["variants"]):
        expected.add("ocr_failure")
    if name["status"] == "conflict" or price["status"] == "conflict":
        status = "conflict"
        if slot["presence"] != "content_candidate":
            return False
    elif slot["presence"] == "unknown":
        status = "unknown"
        expected.add("flat_dark_but_text_or_ocr_failure")
    elif slot["presence"] == "empty_candidate":
        status = "empty_candidate"
        if any(f["status"] != "unknown" or f["candidates"]
               or any(set(v["reasons"]) != {"no_text_in_roi"} for v in f["variants"])
               for f in (name, price)):
            return False
        expected.add("dark_low_variance_and_all_ocr_blank")
    elif name["status"] == price["status"] == "candidate" and not expected:
        status = "candidate"
    else:
        status = "unknown"
    if status in ("unknown", "conflict"):
        expected.update(name["reasons"])
        expected.update(price["reasons"])
    return slot["status"] == status and reasons == expected


def unknown_shop(frame, reason):
    return {"status": "unknown", "reason": reason, "input_frame_ref": frame["frame_ref"],
            "input_source_sha256": frame["source_sha256"], "matched_source_sha256": None,
            "completion_verified": False, "source_kind": "historical_replay", "usage": "development_only",
            "execution_eligible": False, "automatic_action": None, "rule_binding": "unverified",
            "prices_used_for_economy": False,
            "slots": [_unknown_slot(slot, reason) for slot in range(1, 6)]}


def match_shop(frame, document, config, image_size, current_hashes, *, completion_verified=False):
    def fail(reason):
        return unknown_shop(frame, reason)

    if not completion_verified or not isinstance(config, dict) or not isinstance(document, dict):
        return fail("shop_completed_run_unavailable")
    summary = document.get("summary")
    if (type(document.get("revision")) is not int or document["revision"] != 1
            or not isinstance(summary, dict) or type(summary.get("revision")) is not int
            or summary["revision"] != 1 or summary.get("format") != "shop-probe-private-run-v1"):
        return fail("shop_revision_or_format_mismatch")
    run_id = config.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip() or summary.get("run_id") != run_id:
        return fail("shop_run_id_mismatch")
    records = document.get("frames")
    if (not isinstance(records, list) or type(summary.get("frames")) is not int
            or len(records) != summary["frames"] or any(not isinstance(r, dict) for r in records)):
        return fail("shop_frame_format_invalid")
    hashes = [r.get("source_sha256") for r in records]
    if (any(not isinstance(h, str) or not re.fullmatch(r"[a-f0-9]{64}", h) for h in hashes)
            or len(set(hashes)) != len(hashes) or len(set(current_hashes)) != len(current_hashes)):
        return fail("shop_duplicate_or_invalid_source")
    selected = [r for r in records if r["source_sha256"] == frame["source_sha256"]]
    if len(selected) != 1 or current_hashes.count(frame["source_sha256"]) != 1:
        return fail("shop_source_not_found")
    record = selected[0]
    if (record.get("format") != "shop-probe-v1" or record.get("usage") != "offline_development_only"
            or record.get("actionable", True) is not False or record.get("accuracy", "missing") is not None
            or record.get("issues") != [] or record.get("device_capture_time", "missing") is not None):
        return fail("shop_frame_contract_invalid")
    if (not isinstance(record.get("layout_id"), str) or not record["layout_id"].strip()
            or record["layout_id"] != config.get("layout_id")
            or not isinstance(record.get("image_size"), list)
            or any(type(n) is not int for n in record["image_size"])
            or record["image_size"] != list(image_size)):
        return fail("shop_layout_or_geometry_mismatch")
    slots = record.get("slots")
    if (not isinstance(slots, list) or len(slots) != 5
            or any(not isinstance(s, dict) or type(s.get("slot")) is not int for s in slots)
            or [s["slot"] for s in slots] != [1, 2, 3, 4, 5]):
        return fail("shop_slot_mapping_invalid")
    mapped, scale_sequences = [], set()
    for slot in slots:
        name, price = slot.get("name"), slot.get("price")
        reasons = slot.get("reasons")
        if (not isinstance(slot.get("status"), str) or slot["status"] not in SLOT_STATES
                or not isinstance(name, dict) or not isinstance(price, dict)
                or slot.get("identity_verified", True) is not False
                or slot.get("visibility") != "unverified" or slot.get("font_legibility") != "unverified"
                or not isinstance(slot.get("presence"), str)
                or slot["presence"] not in {"empty_candidate", "content_candidate", "unknown"}
                or not isinstance(slot.get("occlusion"), str)
                or slot["occlusion"] not in {"suspected", "not_detected", "unknown"}
                or not isinstance(slot.get("reference_cost_check"), str)
                or slot["reference_cost_check"] not in {"not_comparable", "agrees_with_unverified_reference",
                                                       "differs_from_unverified_reference"}
                or not _codes(reasons)):
            return fail("shop_slot_contract_invalid")
        name_scales, price_scales = _field_scales(name, "name"), _field_scales(price, "price")
        if name_scales is not None and name_scales == price_scales:
            scale_sequences.add(name_scales)
        # Any overlay declaration blocks both fields, even if the summaries disagree.
        if (slot["status"] == "occluded_candidate" or slot["occlusion"] == "suspected"
                or "body_overlay_text_cue" in reasons):
            mapped.append(_unknown_slot(slot["slot"], "body_overlay_text_cue", status="occluded_candidate"))
            continue
        labels = _name_labels(name, slot.get("name_options")) if name_scales is not None else None
        if (name_scales is None or name_scales != price_scales or labels is None
                or not _slot_consistent(slot)):
            mapped.append(_unknown_slot(slot["slot"], "shop_slot_evidence_inconsistent"))
            continue
        if slot["presence"] == "unknown":
            mapped.append(_unknown_slot(slot["slot"], "flat_dark_but_text_or_ocr_failure"))
            continue
        names, name_status = [], name["status"]
        if slot["status"] in ("empty_candidate", "conflict"):
            name_status = "conflict" if slot["status"] == "conflict" else "unknown"
        elif name_status == "candidate":
            names = labels
        mapped.append({"slot": slot["slot"], "status": slot["status"], "name_status": name_status,
                       "names": names, "price_status": price["status"], "identity_verified": False,
                       "reason_codes": list(reasons)})
    if len(scale_sequences) > 1:
        mapped = [s if s["status"] == "occluded_candidate" else
                  _unknown_slot(s["slot"], "shop_variant_scales_inconsistent") for s in mapped]
    result = unknown_shop(frame, "")
    elapsed = record.get("elapsed_ms")
    result.update(status="development_candidate", reason=None, completion_verified=True,
                  matched_source_sha256=frame["source_sha256"], slots=mapped,
                  upstream_processing_ms=elapsed if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0 else None)
    return result
