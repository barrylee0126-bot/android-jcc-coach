"""Explicit historical-candidate adapters, without state or action authority."""

import math
import re
import time

from jcc_economy_lab import GoldCandidate, HypotheticalPrice, analyze_purchases, ten_gold_five_cap_hypothesis


CAPACITY_REVISION = "roi-bound-sanitized-variants-v2"


def economy_comparison(frame: dict) -> dict:
    """Map only this frame's nonconflicting numeric gold into a hypothesis.

    This deliberately does not upgrade the unknown semantic gate or self view.
    Prices 1..5 and the historical 10/1/5 rule are explicit chosen assumptions.
    """
    probes = [p for p in frame.get("fields", ()) if p.get("field") == "gold"]
    probe = probes[0] if len(probes) == 1 else None
    variants = [p for v in frame.get("variants", ()) for p in v.get("fields", ()) if p.get("field") == "gold"]
    conflict = (probe is not None and probe.get("reason") == "variant_disagreement"
                or len({p.get("candidate") for p in variants if type(p.get("candidate")) is int}) > 1)
    valid = (frame.get("sample_kind") == "replay" and frame.get("capture_kind") == "screenshot"
             and probe is not None and probe.get("status") == "development_candidate"
             and probe.get("reason") is None and not conflict and len(variants) == 2
             and all(p.get("status") == "development_candidate" and p.get("reason") is None
                     and p.get("candidate") == probe.get("candidate") for p in variants))
    status = "candidate" if valid else "conflict" if conflict else "unknown"
    gold = GoldCandidate(probe.get("candidate") if valid else None, "replay_candidate",
                         frame["frame_ref"] + "/gold", status=status)
    result = analyze_purchases(gold,
                              [HypotheticalPrice(value, label=f"独立假设价格 {value}") for value in (1, 2, 3, 4, 5)],
                              ten_gold_five_cap_hypothesis())
    return {"input_frame_ref": frame["frame_ref"], "input_source_sha256": frame["source_sha256"],
            "input_mapping": {"from_status": probe.get("status") if probe else "unknown",
                              "to_status": status, "origin": "replay_candidate",
                              "semantic_gate_upgraded": False, "self_subject_verified": False},
            "prices_are": "independent_hypotheses_not_detected_shop_prices",
            "rule_binding": "historical_hypothesis_unverified_for_this_match", "result": result}


def unknown_capacity(frame: dict, reason: str) -> dict:
    return {"status": "unknown", "candidate": None,
            "reason": reason, "reason_codes": [reason],
            "input_frame_ref": frame["frame_ref"], "source_kind": "historical_replay",
            "input_source_sha256": frame["source_sha256"],
            "implementation_revision": None, "matched_source_sha256": None,
            "rule_binding": "unverified", "execution_eligible": False,
            "automatic_action": None,
            "reminder_zh": "本帧上场人数/容量未知，无法判断空位。阶段、主体、备战席是否有可上场棋子均尚未证实。"}


def match_capacity(frame: dict, document, config: dict | None, image_size) -> dict:
    """Join only an exact source digest from the reviewed revision and ROI.

    No position-based join, stale fallback, OCR rerun, or variant value escape.
    Configuration and image digests remain private; arbitrary hint text is not
    copied from the input report into the page or integration result.
    """
    started = time.perf_counter()

    def fail(reason):
        result = unknown_capacity(frame, reason)
        result["join_processing_ms"] = (time.perf_counter() - started) * 1000
        return result

    if not isinstance(config, dict):
        return fail("capacity_results_not_configured")
    if not isinstance(document, dict):
        return fail("capacity_results_unavailable")
    if document.get("schema_version") != "capacity-probe-v1":
        return fail("capacity_schema_mismatch")
    if document.get("implementation_revision") != CAPACITY_REVISION:
        return fail("capacity_revision_mismatch")
    config_hash = config.get("config_sha256")
    if (not isinstance(config_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", config_hash)
            or document.get("config_sha256") != config_hash):
        return fail("capacity_config_mismatch")
    records = document.get("results")
    if (not isinstance(records, list) or type(document.get("frame_count")) is not int
            or document["frame_count"] != len(records)):
        return fail("capacity_report_format_invalid")
    if any(not isinstance(r, dict) or r.get("schema_version") != "capacity-probe-v1"
           or r.get("implementation_revision") != CAPACITY_REVISION for r in records):
        return fail("capacity_frame_revision_mismatch")
    if any(not isinstance(r.get("source_frame"), dict) for r in records):
        return fail("capacity_report_format_invalid")
    matching = [r for r in records if r["source_frame"].get("encoded_sha256") == frame["source_sha256"]]
    if len(matching) != 1:
        return fail("capacity_duplicate_source" if len(matching) > 1 else "capacity_source_not_found")
    record = matching[0]
    source = record["source_frame"]
    size = source.get("image_size")
    if (not isinstance(image_size, (list, tuple)) or len(image_size) != 2
            or any(type(n) is not int or n <= 0 for n in image_size)
            or source.get("source_kind") != "replay" or not isinstance(size, list) or len(size) != 2
            or any(type(n) is not int or n <= 0 for n in size) or size != list(image_size)
            or type(source.get("rotation_applied_degrees")) is not int
            or source["rotation_applied_degrees"] != 0):
        return fail("capacity_source_geometry_mismatch")
    roi = record.get("input_roi")
    expected_roi = config.get("input_roi")
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", value)
           for value in (record.get("layout_id"), config.get("layout_id"))):
        return fail("capacity_layout_id_invalid")
    if (not isinstance(expected_roi, (list, tuple)) or len(expected_roi) != 4
            or any(type(n) is not int for n in expected_roi)
            or not isinstance(roi, list) or len(roi) != 4 or any(type(n) is not int for n in roi)
            or not 0 <= roi[0] < roi[2] <= image_size[0] or not 0 <= roi[1] < roi[3] <= image_size[1]
            or roi != list(expected_roi) or record.get("layout_id") != config.get("layout_id")):
        return fail("capacity_roi_or_layout_mismatch")
    context = record.get("context")
    if (not isinstance(context, dict) or any(context.get(k) != "unknown" for k in
                                           ("phase", "subject", "deployable_bench_unit"))
            or context.get("temporal_stability") != "unverified_single_frame"
            or context.get("review_status") != "unreviewed_machine_agent_candidate"
            or record.get("automatic_action", "missing") is not None
            or record.get("accuracy", "missing") is not None):
        return fail("capacity_authority_or_context_invalid")
    reasons = record.get("reason_codes")
    if (not isinstance(reasons, list) or len(reasons) > 32
            or any(not isinstance(s, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", s) for s in reasons)):
        return fail("capacity_result_format_invalid")
    values = tuple(record.get(k) for k in ("displayed_deployed", "capacity", "empty_slots"))
    confidence = record.get("confidence")
    if (not isinstance(confidence, dict) or set(confidence) != {"calibrated_probability", "basis", "variants"}
            or confidence["calibrated_probability"] is not None
            or confidence["basis"] != "same_apple_vision_backend_scale_comparison"
            or not isinstance(confidence["variants"], list)):
        return fail("capacity_confidence_contract_invalid")
    diagnostics = confidence["variants"]
    diagnostic_keys = {"scale", "status", "reason", "raw_ocr_score", "text_roi", "processing_seconds"}
    if any(not isinstance(v, dict) or set(v) != diagnostic_keys
           or v["status"] not in ("development_candidate", "unknown")
           or (v["status"] == "development_candidate" and v["reason"] is not None)
           or (v["status"] == "unknown" and (not isinstance(v["reason"], str)
                                             or not re.fullmatch(r"[a-z0-9_]{1,80}", v["reason"])))
           for v in diagnostics):
        return fail("capacity_diagnostic_contract_invalid")
    scales = [v["scale"] for v in diagnostics]
    if (any(type(scale) is not int or not 1 <= scale <= 4 for scale in scales)
            or len(set(scales)) != len(scales)):
        return fail("capacity_variant_scales_invalid")
    if "scales" in config:
        expected_scales = config["scales"]
        if (not isinstance(expected_scales, (list, tuple)) or len(expected_scales) < 2
                or any(type(s) is not int or not 1 <= s <= 4 for s in expected_scales)
                or len(set(expected_scales)) != len(expected_scales)
                or diagnostics and scales != list(expected_scales)):
            return fail("capacity_variant_scales_mismatch")
    if record.get("status") == "unknown":
        if any(v is not None for v in values) or not reasons:
            return fail("capacity_unknown_has_values_or_no_reason")
        result = unknown_capacity(frame, "capacity_upstream_unknown")
        result["reason_codes"] = list(reasons)
    elif record.get("status") == "development_candidate":
        if (reasons or len(diagnostics) < 2
                or any(v["status"] != "development_candidate" for v in diagnostics)):
            return fail("capacity_candidate_conflict_or_incomplete")
        n, c, empty = values
        if (any(type(v) is not int for v in values) or not 0 <= n <= c <= 999999
                or c == 0 or empty != c - n):
            return fail("capacity_candidate_arithmetic_invalid")
        hint = (f"本帧机器候选空位为{empty}；若为己方备战且备战席有可用棋子，可检查空位。" if empty else
                "本帧机器候选空位为0。")
        result = {**unknown_capacity(frame, ""), "status": "development_candidate",
                  "candidate": {"displayed_deployed": n, "capacity": c, "empty_slots": empty},
                  "reason": None, "reason_codes": [],
                  "reminder_zh": hint + "阶段、主体、备战席是否有可上场棋子均尚未证实。"}
    else:
        return fail("capacity_status_invalid")
    elapsed = record.get("processing_seconds")
    result.update(implementation_revision=CAPACITY_REVISION, matched_source_sha256=frame["source_sha256"],
                  input_roi_verified=True, config_sha256_verified=True, source_match="exact_encoded_sha256",
                  context={k: context[k] for k in ("phase", "subject", "deployable_bench_unit", "temporal_stability", "review_status")},
                  upstream_processing_ms=elapsed * 1000 if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0 else None,
                  join_processing_ms=(time.perf_counter() - started) * 1000)
    return result
