"""Narrow adapters for the fixed reports used by this local review."""

from __future__ import annotations

from jcc_replay_demo.integrations import match_capacity

from .private import ReviewError


def row(label, authority, candidate, source_hash, *, reasons=(), explanation=""):
    return {"label": label, "authority": authority,
            "status": "development_candidate" if candidate is not None else "unknown",
            "candidate": candidate, "reason_codes": list(reasons), "explanation": explanation,
            "source_sha256": source_hash}


def capacity_evidence(spec, inputs, *, source_hash, size, image_pin):
    """Reuse 17A's revision/config/ROI/scale/unknown/arithmetic validator."""
    config = inputs.json(spec["config_pin"])
    document = inputs.json(spec["report_pin"])
    entries = [entry for entry in config.get("frames", []) if entry.get("expected_sha256") == source_hash]
    if len(entries) != 1 or inputs.path(image_pin) != inputs.path(spec.get("image_pin", image_pin)):
        raise ReviewError("capacity_config_frame_mismatch")
    entry = entries[0]
    if str(inputs.path(image_pin)) != entry.get("image_path"):
        raise ReviewError("capacity_config_image_mismatch")
    layout = config.get("layout", {})
    evidence = entry.get("evidence", {})
    roi = layout.get("roi")
    if (layout.get("image_size") != size or evidence.get("encoded_sha256") != source_hash
            or evidence.get("roi") != roi or evidence.get("image_size") != size
            or evidence.get("layout_id") != layout.get("layout_id")
            or evidence.get("assessment_method") != "agent_development_observation"):
        raise ReviewError("capacity_declaration_binding_mismatch")
    joined = match_capacity({"frame_ref": "sha256:" + source_hash, "source_sha256": source_hash}, document,
        {"config_sha256": inputs.pins[spec["config_pin"]]["sha256"], "layout_id": layout.get("layout_id"),
         "input_roi": roi, "scales": layout.get("scales")}, size)
    # A forged successful summary cannot override an occluded/unknown caller declaration.
    declaration_allows = (evidence.get("layout_matches") is True
                          and evidence.get("visibility") == "clear_candidate"
                          and evidence.get("animation") == "not_observed_candidate")
    if joined["candidate"] is not None and not declaration_allows:
        raise ReviewError("capacity_candidate_overrides_blocker")
    value = joined["candidate"]
    candidate = (f"{value['displayed_deployed']}/{value['capacity']}（显示差值 {value['empty_slots']}）"
                 if value is not None else None)
    reasons = joined.get("reason_codes", [])
    explanation = ("同一 Apple Vision 的两倍率候选；显示差值不证明实际棋子数或可操作性。"
                   if candidate is not None else spec.get("unknown_explanation", "上游拒绝或输入验证未通过，保留未知。"))
    observation = row(spec["label"], "machine_candidate", candidate, source_hash,
                      reasons=reasons, explanation=explanation)
    visibility = {"clear_candidate": "单帧未观察到遮挡", "occluded": "区域受遮挡", "unknown": None}.get(evidence.get("visibility"))
    witness = row(spec["label"] + " · 可见性声明", "agent_visual", visibility, source_hash,
                  reasons=["caller_declaration_not_automatic_detection"],
                  explanation="上游调用方的 Agent 单帧声明；程序没有自动判断遮挡，未建立时间稳定性。")
    region = {"label": spec["label"], "roi": roi, "selection": spec["selection"], "source_sha256": source_hash}
    return [observation, witness], [region], [spec["config_pin"], spec["report_pin"]]


def build_frame(spec, inputs, *, source_hash, size, video_hash, raw=None):
    """Specific report adapters are added here, rather than loosening 17A input."""
    observations, regions, provenance = [], [], []
    image_description = "本局完整原帧；下方局部均由此图裁切。"
    if "source" in spec:
        from .reports import read_20a, read_20b_gold, read_20b_window
        kind = spec["source"].get("kind")
        if kind == "20a":
            observations, regions, provenance, recording_time, image_description = read_20a(spec, inputs, raw, source_hash, size, video_hash)
        elif kind in ("20b_window", "20b_gold"):
            reader = read_20b_window if kind == "20b_window" else read_20b_gold
            observations, regions, provenance, recording_time, image_description = reader(spec, inputs, source_hash, size, video_hash)
        else:
            raise ReviewError("unsupported_report_adapter")
    else:
        recording_time = spec["recording_time"]
        if recording_time.get("basis_kind") != "documented_ffmpeg_request":
            raise ReviewError("recording_time_binding_required")
    for source in spec.get("capacity_sources", []):
        rows, crops, pins = capacity_evidence(source, inputs, source_hash=source_hash, size=size,
                                              image_pin=spec["image_pin"])
        observations.extend(rows)
        regions.extend(crops)
        provenance.extend(pins)
    # Agent comparison records must be explicitly hash-bound, never machine truth.
    for visual in spec.get("agent_visual", []):
        if visual.get("source_sha256") != source_hash:
            raise ReviewError("agent_visual_source_mismatch")
        observations.append(row(visual["label"], "agent_visual", visual.get("candidate"), source_hash,
                                 reasons=["agent_observation_not_human_truth"], explanation=visual["explanation"]))
    for pin in spec.get("supporting_pins", []):
        inputs.read(pin)
        provenance.append(pin)
    observations.append(row("正式阶段／棋盘归属／动作建议", "missing", None, source_hash,
                            reasons=["independent_context_and_rule_evidence_missing"],
                            explanation="缺少本帧独立复核的阶段、主体与规则依据；不生成建议或操作。"))
    return {"frame_id": spec["frame_id"], "title": spec["title"], "period": spec["period"], "note": spec["note"],
            "source_sha256": source_hash, "video_sha256": video_hash, "image_size": size,
            "sample_kind": "replay", "capture_kind": "video_frame", "freshness": "not_live",
            "image_description": image_description,
            "recording_time": recording_time, "observations": observations, "regions": regions,
            "provenance": [{"pin": key, "sha256": inputs.pins[key]["sha256"]} for key in dict.fromkeys(provenance)]}
