"""Adapters only for 20A masked-canvas and 20B fixed offline report formats."""

from __future__ import annotations

import io
import math

from PIL import Image

from jcc_replay_demo.gold import check_gold_glyph
from jcc_replay_demo.integrations import CAPACITY_REVISION, match_capacity
from .adapters import row
from .private import ReviewError, digest


def one(records, predicate, code):
    selected = [record for record in records if predicate(record)]
    if len(selected) != 1:
        raise ReviewError(code)
    return selected[0]


def completed_files(inputs, completed_pin, pins, *, mode):
    document = inputs.json(completed_pin)
    if mode == "20a":
        if document.get("status") != "complete_offline_development_validation":
            raise ReviewError("20a_not_completed")
        hashes = document.get("files_sha256", {})
    else:
        hashes = {entry["path"]: entry["sha256"] for entry in document.get("files", [])}
    root = inputs.path(completed_pin).parent
    for pin in pins:
        path = inputs.path(pin)
        if not path.is_relative_to(root) or hashes.get(path.relative_to(root).as_posix()) != inputs.pins[pin]["sha256"]:
            raise ReviewError("completed_file_binding_mismatch")


def cached_capacity(record, *, config_hash, roi, layout_id, source_hash, size, scales=(1, 2)):
    """Normalize an embedded record, then use the existing 17A validator."""
    document = {"schema_version": "capacity-probe-v1", "implementation_revision": CAPACITY_REVISION,
                "config_sha256": config_hash, "frame_count": 1, "results": [record]}
    joined = match_capacity({"frame_ref": "sha256:" + source_hash, "source_sha256": source_hash}, document,
                            {"config_sha256": config_hash, "input_roi": roi,
                             "layout_id": layout_id, "scales": list(scales)}, size)
    candidate = joined["candidate"]
    value = (f"{candidate['displayed_deployed']}/{candidate['capacity']}（显示差值 {candidate['empty_slots']}）"
             if candidate is not None else None)
    return value, joined["reason_codes"]


def _stamp(requested, pts, basis):
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in (requested, pts)):
        raise ReviewError("recording_time_invalid")
    return {"requested_seconds": requested, "decoded_pts_seconds": pts, "basis": basis,
            "basis_kind": "hash_bound_extraction_index"}


def read_20a(spec, inputs, raw, source_hash, size, video_hash):
    src = spec["source"]
    names = ("manifest_pin", "result_pin", "config_pin", "observations_pin", "baseline_pin")
    pins = [src[name] for name in names] + [spec["image_pin"]]
    completed_files(inputs, src["completed_pin"], pins, mode="20a")
    manifest, result, config, observations, baseline = [inputs.json(src[name]) for name in names]
    if (manifest.get("schema_version") != "20a-native-pts-and-masked-roi-v1"
            or result.get("schema_version") != "20a-existing-modules-v1"
            or result.get("manifest_sha256") != inputs.pins[src["manifest_pin"]]["sha256"]
            or result.get("config_sha256") != inputs.pins[src["config_pin"]]["sha256"]
            or result.get("observations_sha256") != inputs.pins[src["observations_pin"]]["sha256"]
            or baseline.get("source", {}).get("match-recovered.mkv", {}).get("sha256") != video_hash):
        raise ReviewError("20a_document_binding_mismatch")
    meta = one(manifest["frames"], lambda f: f.get("analysis_frame_sha256") == source_hash, "20a_frame_binding_mismatch")
    frame = one(result["frames"], lambda f: f.get("analysis_input_sha256") == source_hash, "20a_result_binding_mismatch")
    visual = one(observations["frames"], lambda f: f.get("analysis_frame_sha256") == source_hash, "20a_visual_binding_mismatch")
    if (meta["id"] != frame["id"] or meta["id"] != visual["id"] or meta["native_size"] != size
            or (inputs.path(src["manifest_pin"]).parent / meta["analysis_frame_path"]) != inputs.path(spec["image_pin"])
            or frame["actual_pts_seconds"] != meta["actual_pts_seconds"]
            or visual["actual_pts_seconds"] != meta["actual_pts_seconds"]
            or frame.get("human_truth") is not False or frame.get("game_state_emitted") is not False
            or frame.get("actionable") is not False or frame.get("accuracy", "missing") is not None
            or frame["scene"].get("capture_kind") != "video_frame"):
        raise ReviewError("20a_authority_or_time_mismatch")
    rows, regions = [], []
    with Image.open(io.BytesIO(raw)) as image:
        for name in ("round", "gold", "level", "capacity", "phase_visual_only"):
            region = meta["regions"][name]
            with image.crop(region["roi"]) as crop:
                if digest(crop.tobytes()) != region["rgb_sha256"]:
                    raise ReviewError("20a_dependency_pixels_mismatch")
            regions.append({"label": {"round": "回合", "gold": "金币", "level": "等级", "capacity": "容量旧框",
                                       "phase_visual_only": "阶段视觉区（未接机器阶段识别）"}[name],
                            "roi": region["roi"], "selection": "20A 固定开发输入区域", "source_sha256": source_hash})
    variants = frame["variants"]
    if [variant.get("scale") for variant in variants] != [1, 2]:
        raise ReviewError("20a_variant_scales_invalid")
    for name, label in (("round", "回合"), ("gold", "金币"), ("level", "等级")):
        aggregate = one(frame["numbers"], lambda f: f.get("field_name") == name, "20a_numeric_format_invalid")
        proposals = [one(v["fields"], lambda f: f.get("field_name") == name, "20a_numeric_format_invalid") for v in variants]
        value = aggregate["candidate_value"] if aggregate["status"] == "development_candidate" else None
        reasons = list(dict.fromkeys(p["reason"] for p in [aggregate, *proposals] if p.get("reason")))
        if value is not None and (aggregate.get("reason") is not None or any(p["status"] != "development_candidate"
                or p["candidate_value"] != value or p["reason"] is not None
                or p["input_region"] != meta["regions"][name]["roi"] for p in proposals)):
            value, reasons = None, ["20a_candidate_variant_mismatch"]
        if name == "gold":
            expected = check_gold_glyph(raw, roi=meta["regions"][name]["roi"], frame_ref="sha256:" + source_hash,
                                        proposed_value=value)
            if expected != frame["gold_glyph_check"]:
                value, reasons = None, ["gold_glyph_evidence_mismatch"]
        if name == "round" and value is not None:
            value = f"{value['stage']}-{value['step']}"
        rows.append(row(label, "machine_candidate", value, source_hash, reasons=reasons,
                        explanation="20A 固定报告的同源两倍率开发候选；不是正式己方局面。" if value is not None else "上游未通过全部数字规则，未用目视补值。"))
        visual_key = {"round": "round", "gold": "gold_display", "level": "level_display"}[name]
        rows.append(row(label + "显示", "agent_visual", visual.get(visual_key), source_hash,
                        reasons=["agent_observation_not_human_truth"], explanation="20A 在运行 OCR 前记录的 Agent 目视候选。"))
    capacity_value, codes = cached_capacity(frame["capacity"], config_hash=inputs.pins[src["config_pin"]]["sha256"],
        roi=config["capacity_roi"], layout_id=frame["capacity"]["layout_id"], source_hash=source_hash, size=size)
    capacity_evidence = visual["capacity_evidence"]
    if capacity_value is not None and (capacity_evidence.get("roi") != config["capacity_roi"]
            or capacity_evidence.get("visibility") != "clear_candidate"
            or capacity_evidence.get("animation") != "not_observed_candidate"
            or capacity_evidence.get("layout_matches") is not True):
        raise ReviewError("20a_candidate_overrides_blocker")
    rows.append(row("显示容量", "machine_candidate", capacity_value, source_hash, reasons=codes,
                    explanation="固定开发框的容量候选。" if capacity_value else visual["note"]))
    phase = frame["scene"]["phase"]
    rows.append(row("阶段", "machine_candidate", None, source_hash,
                    reasons=[phase.get("reason") or "phase_not_certified"], explanation="现有机器阶段链路没有形成候选；商店可见不能认证准备阶段。"))
    phase_label = {"preparation_label_visible": "可见“准备阶段”横幅", "combat_visual_candidate": "战斗外观候选"}.get(visual["phase"])
    rows.append(row("阶段线索", "agent_visual", phase_label, source_hash,
                    reasons=["agent_context_not_certified"], explanation="与机器阶段结果分开；主体和可操作性仍未知。"))
    return rows, regions, pins + [src["completed_pin"]], _stamp(meta["requested_seconds"], float(meta["actual_pts_seconds"]),
        "20A 完成清单固定的抽帧记录；PTS 为恢复录像解码时间"), "20A 必要原图区域保留的分析画布；黑色区域是主动排除的像素，并非游戏遮挡。"


def read_20b_common(spec, inputs, source_hash, video_hash):
    src = spec["source"]
    pins = [src["source_pin"], src["frame_index_pin"], src["report_pin"], spec["image_pin"]]
    pins += [src[key] for key in ("config_pin", "index_pin", "completed_pin") if key in src]
    completed_files(inputs, src["artifact_pin"], pins, mode="20b")
    source = inputs.json(src["source_pin"])
    if source.get("sha256") != video_hash:
        raise ReviewError("20b_video_binding_mismatch")
    meta = one(inputs.json(src["frame_index_pin"]), lambda f: f.get("sha256") == source_hash, "20b_frame_binding_mismatch")
    root = inputs.path(src["frame_index_pin"]).parent
    if root / meta["file"] != inputs.path(spec["image_pin"]):
        raise ReviewError("20b_frame_path_mismatch")
    stamp = _stamp(meta["requested_time_s"], meta["decoded_pts_s"], "20B 固定帧索引；FFmpeg showinfo 解码 PTS")
    return inputs.json(src["report_pin"]), stamp, pins + [src["artifact_pin"]]


def read_20b_window(spec, inputs, source_hash, size, video_hash):
    src = spec["source"]
    document, stamp, pins = read_20b_common(spec, inputs, source_hash, video_hash)
    config, index, completed = [inputs.json(src[key]) for key in ("config_pin", "index_pin", "completed_pin")]
    if (document.get("schema_version") != "open-slot-probe-v1" or document.get("implementation_revision") != "offline-bound-appearance-v1"
            or completed.get("status") != "completed" or completed.get("input_hashes_unchanged") is not True
            or completed.get("file_sha256", {}).get("results.private.json") != inputs.pins[src["report_pin"]]["sha256"]
            or any(doc.get("config_sha256") != inputs.pins[src["config_pin"]]["sha256"] for doc in (document, completed))
            or any(doc.get("source_index_sha256") != inputs.pins[src["index_pin"]]["sha256"] for doc in (document, completed))):
        raise ReviewError("20b_run_binding_mismatch")
    frame = one(document["results"], lambda f: f.get("source_frame", {}).get("encoded_sha256") == source_hash, "20b_result_binding_mismatch")
    declaration = one(config["frames"], lambda f: f.get("expected_sha256") == source_hash, "20b_config_frame_mismatch")
    indexed = one(index["frames"], lambda f: f.get("encoded_sha256") == source_hash, "20b_index_frame_mismatch")
    if (indexed["asset_path"] != str(inputs.path(spec["image_pin"])) or indexed["decoded_pts_s"] != stamp["decoded_pts_seconds"]
            or declaration["image_path"] != indexed["asset_path"] or declaration["image_size"] != size
            or frame.get("advice_eligible") is not False or frame.get("automatic_action", "missing") is not None
            or frame.get("accuracy", "missing") is not None
            or any(frame["context"].get(key) != "unknown" for key in ("subject", "phase", "rules", "deployable_bench_unit"))):
        raise ReviewError("20b_authority_or_index_mismatch")
    capacity = frame["capacity"]
    if capacity.get("reader_revision") != CAPACITY_REVISION:
        raise ReviewError("20b_capacity_revision_mismatch")
    if len(capacity["trials"]) != 1 or len(declaration["capacity_trials"]) != 1:
        raise ReviewError("20b_single_trial_format_required")
    trial = capacity["trials"][0]
    declared = declaration["capacity_trials"][0]
    evidence, layout = declared["evidence"], declared["layout"]
    if (evidence.get("encoded_sha256") != source_hash or evidence.get("roi") != layout["roi"]
            or trial.get("input_roi") != layout["roi"] or evidence.get("assessment_method") != "agent_development_observation"):
        raise ReviewError("20b_roi_declaration_mismatch")
    native = {**trial, "schema_version": "capacity-probe-v1",
              **{key: capacity[key] for key in ("displayed_deployed", "capacity", "empty_slots")},
              "context": {"phase": "unknown", "subject": "unknown", "deployable_bench_unit": "unknown",
                          "temporal_stability": "unverified_single_frame", "review_status": "unreviewed_machine_agent_candidate"},
              "accuracy": None, "automatic_action": None}
    value, codes = cached_capacity(native, config_hash=inputs.pins[src["config_pin"]]["sha256"], roi=layout["roi"],
                                   layout_id=layout["layout_id"], source_hash=source_hash, size=size)
    if (capacity["status"] != trial["status"] or (value is not None and (capacity["reason_codes"]
            or evidence.get("visibility") != "clear_candidate" or evidence.get("animation") != "not_observed_candidate"
            or evidence.get("layout_matches") is not True))):
        raise ReviewError("20b_candidate_overrides_blocker")
    rows = [row("显示容量", "machine_candidate", value, source_hash, reasons=codes,
                explanation="20B 手调开发 ROI 后的同源两倍率候选。" if value else "本帧拒绝，清空数值；不能沿用前帧差值。"),
            row("备战席合法可部署棋子", "missing", None, source_hash,
                reasons=["appearance_does_not_establish_deployable_unit"], explanation="纹理和物体外观不能证明棋子数量、身份或上场资格。")]
    regions = [{"label": "容量校准框", "roi": layout["roi"], "selection": "20B 看过失败后手动扩大的开发 ROI，非盲验",
                "source_sha256": source_hash}]
    return rows, regions, pins, stamp, "本局完整原帧；下方仅裁切实际输入区域。"


def read_20b_gold(spec, inputs, source_hash, size, video_hash):
    document, stamp, pins = read_20b_common(spec, inputs, source_hash, video_hash)
    result = one(document["results"], lambda f: f.get("frame", {}).get("source_sha256") == source_hash, "20b_gold_binding_mismatch")
    frame = result["frame"]
    if frame.get("requested_time_s") != stamp["requested_seconds"] or frame.get("decoded_pts_s") != stamp["decoded_pts_seconds"]:
        raise ReviewError("20b_gold_time_mismatch")
    # This final upstream report still labels video frames as screenshots. Do
    # not call the screenshot adapter or copy its economic comparisons.
    code = "upstream_capture_kind_mismatch" if frame.get("capture_kind") != "video_frame" else "video_economy_adapter_not_implemented"
    rows = [row("视频金币自动接入", "machine_candidate", None, source_hash, reasons=[code],
                explanation="原报告将本录像帧声明为 screenshot；本页按真实 video_frame 来源拒绝自动接入，未消费其经济比较。"),
            row("条件购买／利息结论", "missing", None, source_hash,
                reasons=["source_adapter_and_match_rule_binding_missing"], explanation="假设支出与现金算术不能转为本局购买建议；本页不显示该探索的利息或支出答案。")]
    regions = [{"label": "金币探索的开发框", "roi": document["input_roi"], "selection": "20B 手调框，仅作来源错误复查",
                "source_sha256": source_hash}]
    return rows, regions, pins, stamp, "本局完整原帧；原报告来源声明错误已在右侧明确阻断。"
