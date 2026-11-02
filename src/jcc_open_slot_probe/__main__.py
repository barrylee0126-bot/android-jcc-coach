"""Explicit private-file runner and self-contained, cropped offline viewer."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import html
import io
import json
import os
from pathlib import Path
import shutil
import stat
import time

from .probe import (METHOD, REVISION, MAX_BYTES, _region_gate, box, decode,
                    digest, identifier, probe_frame)

PROJECT = Path(__file__).resolve().parents[2]


def _no_links(path):
    path = Path(os.path.abspath(path.expanduser()))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("symlink_path_rejected")
    return path


def _output_path(path, inputs):
    target = _no_links(path)
    if target.exists() or target in inputs or target == PROJECT or PROJECT in target.parents:
        raise ValueError("new_private_output_outside_checkout_required")
    parent = target.parent
    if not parent.is_dir() or stat.S_IMODE(parent.stat().st_mode) != 0o700 or parent.stat().st_uid != os.getuid():
        raise ValueError("existing_private_parent_0700_required")
    return target


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _write_new(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(payload)


def _input(path, expected, inputs, *, max_bytes=MAX_BYTES):
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ValueError("absolute_private_input_required")
    path = _no_links(path)
    if path == PROJECT or PROJECT in path.parents:
        raise ValueError("private_inputs_outside_checkout_required")
    if not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
        raise ValueError("explicit_readable_input_required")
    payload = path.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if expected is not None and actual != digest(expected):
        raise ValueError("input_hash_mismatch")
    if path in inputs and inputs[path] != actual:
        raise ValueError("input_changed_during_preflight")
    inputs[path] = actual
    return payload


def _preview(payload, roi, detections):
    with decode(payload) as im:
        roi = box(roi, im.size)
        with im.crop(roi) as crop:
            encoded = io.BytesIO()
            crop.save(encoded, format="PNG")
    data = encoded.getvalue()
    sha = hashlib.sha256(payload).hexdigest()
    meta = {"source_encoded_sha256": sha, "roi": roi,
            "derived_png_sha256": hashlib.sha256(data).hexdigest(),
            "derivation": "decode_RGB_then_exact_crop_then_PNG_no_pixel_edits"}
    width, height = roi[2] - roi[0], roi[3] - roi[1]
    marks = []
    for d in detections:
        l, t, r, b = d["roi"]
        l, t, r, b = max(l, roi[0]), max(t, roi[1]), min(r, roi[2]), min(b, roi[3])
        if r <= l or b <= t:
            continue
        color = {"model_appearance": "#34d399", "resource_appearance": "#fbbf24"}.get(d.get("class"), "#c084fc")
        marks.append(f'<rect x="{l-roi[0]}" y="{t-roi[1]}" width="{r-l}" height="{b-t}" fill="none" stroke="{color}" stroke-width="4"/>')
    svg = (f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="同帧裁切与外观候选框">'
           f'<image width="{width}" height="{height}" href="data:image/png;base64,{base64.b64encode(data).decode()}"/>'
           + "".join(marks) + '</svg>')
    return meta, svg


def _card(row, payload):
    source, capacity, bench = row["source_frame"], row["capacity"], row["bench"]
    previews, panels = {}, []
    if capacity and capacity["trials"]:
        roi = capacity["trials"][0]["input_roi"]
        meta, svg = _preview(payload, roi, [])
        previews["capacity"] = meta
        panels.append('<div><h3>容量原始裁切</h3>' + svg + '</div>')
    if bench and bench["input_roi"]:
        meta, svg = _preview(payload, bench["input_roi"], bench["matches"])
        previews["bench"] = meta
        panels.append('<div><h3>备战席原始裁切 · 绿框模型外观 / 黄框资源外观</h3>' + svg + '</div>')
    row["previews"] = previews
    capacity_text = "未知"
    if capacity and capacity["status"] == "development_candidate":
        capacity_text = f'{capacity["displayed_deployed"]}/{capacity["capacity"]}，候选差值 {capacity["empty_slots"]}'
    bench_text = "未知" if not bench else f'物体 {bench["object_presence"]}；模型外观 {bench["model_like_presence"]}；资源外观 {bench["resource_like_presence"]}'
    reasons = [] if not capacity else list(capacity["reason_codes"])
    if bench:
        reasons += bench["reason_codes"]
    # Copying above is important: viewer rendering must not mutate probe reasons.
    text = (f'<article id="{source["alias"]}"><h2>{source["alias"]} · {source["viewing_group"]}</h2>'
            f'<p class="hint">{html.escape(row["review_only_hint"])}</p>'
            f'<p>容量：{capacity_text}<br>备战席：{html.escape(bench_text)}</p>'
            f'<p class="small">{html.escape(", ".join(reasons))}<br>'
            f'完整原图 SHA-256：{source["encoded_sha256"]}<br>'
            f'外层活动：{source["collection_activity"]} · 离线处理 {row["seconds"]:.3f} 秒</p>'
            '<div class="panels">' + "".join(panels) + '</div></article>')
    return text


def _viewer(cards, report):
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'">'
            '<title>17G 离线空位与备战席外观线索</title>'
            '<style>body{margin:28px auto;padding:0 20px;max-width:1400px;background:#0f172a;color:#e2e8f0;font:16px/1.6 system-ui}h1,h2,h3{line-height:1.3}h3{font-size:15px}.hint{color:#fde68a}.small{font-size:13px;overflow-wrap:anywhere;color:#94a3b8}article{border:1px solid #334155;border-radius:12px;padding:20px;margin:25px 0}.panels{display:grid;grid-template-columns:minmax(240px,1fr) minmax(400px,2fr);gap:16px}svg{width:100%;display:block;background:#111827}@media(max-width:800px){.panels{display:block}}</style>'
            '<h1>17G · 仅供复盘的外观线索</h1>'
            '<p class="hint">全部为历史开发图和未复核机器/Agent 候选。主体、可操作阶段、合法棋子、规则及连续窗口均未知；不产生上场建议或动作。</p>'
            '<p>原图只读；预览由本次参与检测的同一字节解码裁切。每张原图摘要、裁切范围和派生 PNG 摘要均在 results.private.json 中。模型模板相似只表示像已有外观，不能证明物体身份或归属。未匹配不表示没有棋子。</p>'
            f'<p class="small">配置摘要 {report["config_sha256"]} · {report["frame_count"]} 张开发原图 · 模板及阈值均未经真实准确率校准。没有真人标签或不同人员独立复核。</p>'
            + "".join(cards) + '</html>')


def run_config(config_path, *, output, ocr_factory=None):
    """Pin/read an explicit file allowlist; never discovers or writes datasets."""
    started = time.perf_counter()
    inputs = {}
    config_path = Path(config_path).expanduser()
    config_bytes = _input(config_path, None, inputs, max_bytes=2 * 1024 * 1024)
    config = json.loads(config_bytes)
    if config.get("schema_version") != METHOD:
        raise ValueError("invalid_config_schema")
    frames = config.get("frames")
    refs = config.get("references", [])
    if not isinstance(frames, list) or not 1 <= len(frames) <= 128 or not isinstance(refs, list) or len(refs) > 32:
        raise ValueError("explicit_bounded_allowlist_required")
    target = _output_path(Path(output), {config_path})
    source_index_sha = None
    indexed = None
    if any(frame.get("source_kind") == "replay" for frame in frames) or "source_index" in config:
        index_spec = config["source_index"]
        index_bytes = _input(index_spec["path"], index_spec["expected_sha256"], inputs,
                             max_bytes=2 * 1024 * 1024)
        source_index_sha = hashlib.sha256(index_bytes).hexdigest()
        rows = json.loads(index_bytes)["frames"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 512:
            raise ValueError("invalid_explicit_source_index")
        indexed = {identifier(row["alias"]): row for row in rows}
        if len(indexed) != len(rows):
            raise ValueError("duplicate_source_index_alias")
    payloads, seen, aliases = [], set(), set()
    read_started = time.perf_counter()
    for frame in frames:
        alias = identifier(frame["alias"])
        sha = digest(frame["expected_sha256"])
        if alias in aliases or sha in seen:
            raise ValueError("duplicate_alias_or_source_image")
        aliases.add(alias)
        seen.add(sha)
        if indexed is not None:
            origin = indexed.get(alias)
            if (origin is None or origin["encoded_sha256"] != sha
                    or origin["source"] != frame["viewing_group"]
                    or _no_links(Path(origin["asset_path"])) != _no_links(Path(frame["image_path"]))):
                raise ValueError("frame_source_index_mismatch")
        payloads.append(_input(frame["image_path"], sha, inputs))
    references = []
    ref_ids = set()
    for ref in refs:
        ref_id = identifier(ref["reference_id"])
        if ref_id in ref_ids or ref["class"] not in ("model_appearance", "resource_appearance"):
            raise ValueError("invalid_reference")
        ref_ids.add(ref_id)
        if indexed is not None and not any(
            row["encoded_sha256"] == ref["expected_sha256"]
            and row["source"] == ref["viewing_group"]
            and _no_links(Path(row["asset_path"])) == _no_links(Path(ref["image_path"]))
            for row in indexed.values()
        ):
            raise ValueError("reference_source_index_mismatch")
        data = _input(ref["image_path"], ref["expected_sha256"], inputs)
        with decode(data) as im:
            if ref["source_kind"] not in ("replay", "synthetic"):
                raise ValueError("invalid_reference_source_kind")
            if _region_gate(data, im.size, ref["source_kind"], ref):
                raise ValueError("reference_visibility_or_binding_unknown")
            region = box(ref["roi"], im.size)
            if min(region[2] - region[0], region[3] - region[1]) < 8:
                raise ValueError("reference_too_small")
            crop = im.crop(region)
        references.append({"crop": crop, "metadata": {
            "reference_id": ref_id, "class": ref["class"],
            "encoded_sha256": ref["expected_sha256"], "roi": region,
            "viewing_group": identifier(ref["viewing_group"]),
            "collection_activity": identifier(ref["collection_activity"]),
            "assessment_method": ref["evidence"]["assessment_method"],
            "source_kind": ref["source_kind"], "image_size": list(im.size),
            "layout_id": ref["layout_id"], "label_authority": "unreviewed_development_exemplar",
        }})
    read_seconds = time.perf_counter() - read_started
    # Recheck output after the full input list is known. No inputs are targets.
    target = _output_path(target, set(inputs))
    if ocr_factory is None:
        from jcc_device.vision_ocr import VisionOCR
        ocr_factory = VisionOCR
    cards, results = [], []
    request_seconds = []

    class TimedOCR:
        def __init__(self, backend):
            self.backend = backend

        def recognize(self, payload):
            t0 = time.perf_counter()
            try:
                return self.backend.recognize(payload)
            finally:
                request_seconds.append(time.perf_counter() - t0)

    worker_started = time.perf_counter()
    try:
        with ocr_factory() as backend:
            startup_seconds = time.perf_counter() - worker_started
            ocr = TimedOCR(backend)
            for frame, payload in zip(frames, payloads):
                row = probe_frame(payload, frame, references, ocr)
                cards.append(_card(row, payload))
                results.append(row)
    finally:
        for ref in references:
            ref["crop"].close()
    report = {"schema_version": METHOD, "implementation_revision": REVISION,
              "run_utc": datetime.now(timezone.utc).isoformat(),
              "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
              "source_index_sha256": source_index_sha,
              "source_index_authority": "hash_bound_caller_discovery_index_not_authenticated_recording",
              "frame_count": len(results), "source_recording_independence": "not_established",
              "experiment_role": "exposed_agent_selected_development_images_not_accuracy",
              "human_labels_added": 0, "independent_human_reviews_added": 0,
              "accuracy": None, "advice_eligible": False, "automatic_action": None,
              "timing": {"file_reads_and_reference_preparation_seconds": read_seconds,
                         "worker_process_start_seconds": startup_seconds,
                         "ocr_request_count": len(request_seconds),
                         "first_ocr_request_seconds": request_seconds[0] if request_seconds else None,
                         "ocr_request_seconds": request_seconds,
                         "before_output_write_seconds": time.perf_counter() - started,
                         "scope": "offline_read_decode_detection_diagnostics_and_crop_viewer_not_game_latency"},
              "results": results}
    # Changed inputs cannot obtain a completed run. No source/annotation writes.
    for path, sha in inputs.items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError("input_changed_during_run")
    target.mkdir(mode=0o700)
    try:
        files = {"results.private.json": _json_bytes(report),
                 "查看线索.html": _viewer(cards, report).encode("utf-8")}
        for name, content in files.items():
            _write_new(target / name, content)
        completion = {"schema_version": METHOD, "status": "completed",
                      "config_sha256": report["config_sha256"],
                      "source_index_sha256": source_index_sha,
                      "input_hashes_unchanged": True,
                      "file_sha256": {n: hashlib.sha256(v).hexdigest() for n, v in files.items()},
                      "source_hashes": [r["source_frame"]["encoded_sha256"] for r in results]}
        _write_new(target / "COMPLETED.json", _json_bytes(completion))
    except BaseException:
        shutil.rmtree(target)
        raise
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline capacity/bench appearance discovery; no device or actions.")
    parser.add_argument("--config", type=Path, required=True, help="Private hash-pinned JSON outside checkout")
    parser.add_argument("--output", type=Path, required=True, help="NEW run directory in an existing private 0700 parent outside checkout")
    args = parser.parse_args(argv)
    try:
        report = run_config(args.config, output=args.output)
    except Exception:
        print(json.dumps({"schema_version": METHOD, "status": "unknown",
                          "reason_codes": ["offline_run_failed"], "automatic_action": None}))
        return 2
    print(json.dumps({"schema_version": METHOD, "status": "completed", "frames": report["frame_count"],
                      "seconds": report["timing"]["before_output_write_seconds"],
                      "accuracy": None, "advice_eligible": False, "automatic_action": None}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
