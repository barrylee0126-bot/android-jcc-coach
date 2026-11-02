"""Render a deliberately non-actionable lifecycle from explicit replay evidence.

This module does not capture a device, run OCR, infer a player, or select a
unit.  It only checks caller-supplied, hash-bound stills and makes it visible
when an earlier *conditional* capacity observation must be withdrawn.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat


PROJECT = Path(__file__).resolve().parents[2]
SCHEMA = "hint-replay-v1"
MAX_MANIFEST = 64 * 1024
MAX_IMAGE = 25 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{64}\Z")


class HintReplayError(ValueError):
    pass


def _fail(code: str):
    raise HintReplayError(code)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_json(path: Path) -> dict:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        _fail("manifest_path_invalid")
    if path.resolve().is_relative_to(PROJECT):
        _fail("manifest_must_be_private")
    data = path.read_bytes()
    if not 0 < len(data) <= MAX_MANIFEST:
        _fail("manifest_size_invalid")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("manifest_invalid_json")
    if type(value) is not dict:
        _fail("manifest_invalid_json")
    return value


def _private_image(path_text: object, expected_sha: object) -> tuple[bytes, str]:
    if type(path_text) is not str or type(expected_sha) is not str or not SHA.fullmatch(expected_sha):
        _fail("frame_binding_invalid")
    path = Path(path_text)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        _fail("frame_path_invalid")
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        _fail("frame_path_invalid")
    if resolved.is_relative_to(PROJECT):
        _fail("frame_must_be_private")
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_IMAGE:
            _fail("frame_size_invalid")
        data = path.read_bytes()
    except OSError:
        _fail("frame_unavailable")
    actual = _sha(data)
    if actual != expected_sha:
        _fail("frame_sha256_mismatch")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        _fail("frame_not_png")
    return data, actual


def _frame_state(frame: dict) -> dict:
    status = frame.get("capacity_status")
    if status == "unknown":
        if any(frame.get(key) is not None for key in ("displayed_deployed", "capacity", "empty_slots")):
            _fail("unknown_frame_contains_capacity")
        return {
            "state": "withdrawn_unknown",
            "title": "识别未知：旧条件提示已撤回",
            "detail": "本帧没有可消费的容量候选；不会沿用上一帧的空位差值。",
            "capacity_text": "未知",
            "condition_prompt": None,
        }
    if status != "development_candidate":
        _fail("capacity_status_invalid")
    values = [frame.get(key) for key in ("displayed_deployed", "capacity", "empty_slots")]
    if any(type(value) is not int or value < 0 for value in values):
        _fail("capacity_values_invalid")
    deployed, capacity, empty = values
    if deployed > capacity or empty != capacity - deployed:
        _fail("capacity_values_inconsistent")
    capacity_text = f"{deployed}/{capacity}（差 {empty}）"
    if empty == 0:
        return {
            "state": "withdrawn_full",
            "title": "容量候选满额：旧条件提示保持撤回",
            "detail": "本帧的开发候选没有显示差值；不保留前一帧的空位条件。",
            "capacity_text": capacity_text,
            "condition_prompt": None,
        }
    return {
        "state": "conditional_prompt",
        "title": "条件提示：显示容量候选有空位",
        "detail": "只显示同帧容量候选；主体、阶段、规则与合法可上场单位均未确认。",
        "capacity_text": capacity_text,
        "condition_prompt": "不提供上场对象、买卖或点击建议。",
    }


def _validate_manifest(manifest: dict) -> list[dict]:
    required = {"schema_version", "purpose", "source_kind", "frames"}
    if set(manifest) != required or manifest.get("schema_version") != SCHEMA:
        _fail("manifest_schema_invalid")
    if manifest.get("purpose") != "private_replay_lifecycle_only" or manifest.get("source_kind") != "recovered_recording_development":
        _fail("manifest_scope_invalid")
    frames = manifest.get("frames")
    if type(frames) is not list or len(frames) < 3 or len(frames) > 16:
        _fail("frame_count_invalid")
    previous = -1.0
    out = []
    for index, raw in enumerate(frames):
        if type(raw) is not dict or set(raw) != {"id", "requested_time_s", "decoded_pts_s", "path", "sha256", "capacity_status", "displayed_deployed", "capacity", "empty_slots"}:
            _fail("frame_schema_invalid")
        if type(raw["id"]) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", raw["id"]):
            _fail("frame_id_invalid")
        if type(raw["requested_time_s"]) not in (int, float) or type(raw["decoded_pts_s"]) not in (int, float):
            _fail("frame_time_invalid")
        if raw["requested_time_s"] <= previous or raw["decoded_pts_s"] < 0:
            _fail("frame_time_order_invalid")
        previous = float(raw["requested_time_s"])
        image, source_sha = _private_image(raw["path"], raw["sha256"])
        state = _frame_state(raw)
        out.append({**raw, **state, "source_sha256": source_sha,
                    "image_data_uri": "data:image/png;base64," + base64.b64encode(image).decode("ascii"),
                    "sequence": index + 1})
    expected = ["conditional_prompt", "withdrawn_unknown", "withdrawn_full"]
    if [row["state"] for row in out] != expected:
        _fail("lifecycle_sequence_not_demonstrated")
    return out


def _page(rows: list[dict]) -> str:
    cards = []
    for row in rows:
        prompt = "" if row["condition_prompt"] is None else f"<p class='prompt'>{html.escape(row['condition_prompt'])}</p>"
        cards.append(f"""<article class='{html.escape(row['state'])}' id='{html.escape(row['id'])}'>
<header><span>第 {row['sequence']} 帧</span><strong>{html.escape(row['title'])}</strong></header>
<img alt='私有回放原帧' src='{row['image_data_uri']}' />
<dl><dt>请求时间</dt><dd>{row['requested_time_s']:.0f}s</dd><dt>解码 PTS</dt><dd>{row['decoded_pts_s']:.3f}s</dd><dt>容量机器候选</dt><dd>{html.escape(row['capacity_text'])}</dd><dt>原帧摘要</dt><dd class='hash'>{row['source_sha256']}</dd></dl>
<p>{html.escape(row['detail'])}</p>{prompt}</article>""")
    return """<!doctype html><meta charset='utf-8'><meta http-equiv='Content-Security-Policy' content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>本局容量提示撤回回放</title><style>
body{background:#10151d;color:#e7edf4;font:16px/1.55 system-ui;margin:0 auto;max-width:1260px;padding:32px}h1{margin-top:0} .note{color:#bcc8d5;max-width:900px}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}article{border:1px solid #425164;border-radius:14px;padding:14px;background:#17212d}article.conditional_prompt{border-color:#4eaf77}article.withdrawn_unknown{border-color:#d69d51}article.withdrawn_full{border-color:#6e91c7}header{display:grid;gap:7px;color:#afbed0}strong{font-size:18px;color:#fff}img{width:100%;margin:12px 0;border-radius:8px;background:#000}dl{display:grid;grid-template-columns:auto 1fr;gap:5px 10px;font-size:13px}dt{color:#9fb0c2}dd{margin:0}.hash{font:11px/1.25 ui-monospace;word-break:break-all;color:#9fb0c2}.prompt{color:#ffe2a9;font-weight:600}@media(max-width:900px){.grid{grid-template-columns:1fr}}</style>
<h1>本局容量候选：提示出现与撤回回放</h1><p class='note'>离线私有回放演示。第一帧只显示“容量候选有空位”的条件提示；第二帧识别未知时立即撤回；第三帧满额候选时保持撤回。页面不识别主体、阶段、规则或合法单位，不给上场、买卖或点击建议。</p>
<div class='grid'>""" + "".join(cards) + "</div><p class='note'>三帧来自同一录像，容量区域曾在首次失败后人工扩大；它是开发回放，不是盲验、实时链路、准确率或游戏表现。</p>"


def _private_output(path: Path) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink() or path.resolve().is_relative_to(PROJECT):
        _fail("output_path_invalid")
    if path.parent.is_symlink() or not path.parent.is_dir():
        _fail("output_parent_invalid")
    os.mkdir(path, 0o700)


def build_replay(manifest_path: str | Path, output_directory: str | Path) -> dict:
    """Validate exactly three replay frames and render a self-contained private page."""
    manifest = _load_json(Path(manifest_path))
    rows = _validate_manifest(manifest)
    output = Path(output_directory)
    _private_output(output)
    html_bytes = _page(rows).encode("utf-8")
    report = {
        "schema_version": SCHEMA,
        "scope": "private_replay_lifecycle_only",
        "human_ground_truth_count": 0,
        "independent_review_count": 0,
        "accuracy": None,
        "advice_eligible": False,
        "automatic_action": None,
        "frames": [{key: row[key] for key in ("id", "requested_time_s", "decoded_pts_s", "source_sha256", "state", "capacity_text", "title")} for row in rows],
    }
    for name, data in (("report.html", html_bytes), ("review.private.json", json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))):
        destination = output / name
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(data)
    return report
