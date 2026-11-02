"""Render a short private replay sequence with candidate withdrawal.

The caller binds every displayed image by SHA-256.  This module deliberately
does not run OCR, infer a real purchase price, or carry a number into a newer
frame.  It makes a pre-existing machine candidate and an explicit arithmetic
assumption visible for review, then withdraws it when the next frame has no
reliable candidate.
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import re
import stat

from PIL import Image, UnidentifiedImageError


PROJECT = Path(__file__).resolve().parents[2]
SCHEMA = "replay-coach-v1"
MAX_MANIFEST_BYTES = 96 * 1024
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
WITHDRAWAL_REASONS = {
    "new_frame_without_reliable_candidate", "candidate_expired", "scene_transition",
    "source_disconnected", "frame_binding_missing", "candidate_conflict",
}


class ReplayCoachError(ValueError):
    """A fixed, non-sensitive error code."""


def _fail(code: str) -> None:
    raise ReplayCoachError(code)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _fail("duplicate_json_key")
        value[key] = item
    return value


def _private_path(path: Path, *, directory: bool = False) -> Path:
    if not path.is_absolute() or path.is_symlink():
        _fail("private_path_invalid")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError:
        _fail("private_path_invalid")
    if resolved == PROJECT or PROJECT in resolved.parents:
        _fail("private_path_inside_project")
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or info.st_uid != os.getuid() or info.st_mode & 0o077:
        _fail("private_path_invalid")
    return resolved


def _read_private(path: Path, maximum: int) -> bytes:
    resolved = _private_path(path)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(resolved, flags)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not 0 < before.st_size <= maximum:
                _fail("private_file_size_invalid")
            data = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
    except ReplayCoachError:
        raise
    except OSError:
        _fail("private_file_unavailable")
    if (len(data) != before.st_size or len(data) > maximum
            or (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size)
            != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)):
        _fail("private_file_changed")
    return data


def _load_manifest(path: Path) -> dict:
    data = _read_private(path, MAX_MANIFEST_BYTES)
    try:
        value = json.loads(data, object_pairs_hook=_object_pairs,
                           parse_constant=lambda _value: _fail("nonfinite_json"))
    except ReplayCoachError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        _fail("manifest_invalid_json")
    if type(value) is not dict:
        _fail("manifest_invalid_json")
    return value


def _bound_png(path_text: object, digest: object) -> tuple[bytes, str]:
    if type(path_text) is not str or type(digest) is not str or not SHA256.fullmatch(digest):
        _fail("frame_binding_invalid")
    data = _read_private(Path(path_text), MAX_IMAGE_BYTES)
    actual = _sha256(data)
    if actual != digest:
        _fail("frame_sha256_mismatch")
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        _fail("frame_not_png")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if (image.format != "PNG" or getattr(image, "n_frames", 1) != 1
                    or image.width * image.height > MAX_IMAGE_PIXELS):
                _fail("frame_not_png")
            image.verify()
    except ReplayCoachError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        _fail("frame_not_png")
    return data, actual


def _finite_time(value: object) -> float:
    if type(value) not in (int, float) or isinstance(value, bool):
        _fail("frame_time_invalid")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        _fail("frame_time_invalid")
    if not math.isfinite(number):
        _fail("frame_time_invalid")
    return number


def _candidate(raw: object, frame_sha: str) -> dict:
    if type(raw) is not dict:
        _fail("candidate_schema_invalid")
    status = raw.get("status")
    if status == "unknown":
        if (set(raw) != {"status", "reason", "basis"} or type(raw["reason"]) is not str
                or raw["reason"] not in WITHDRAWAL_REASONS
                or raw["basis"] != "no_fresh_candidate_supplied"):
            _fail("unknown_candidate_invalid")
        return {"state": "withdrawn_unknown", "candidate": None,
                "source": None, "spend": None, "balance_after": None,
                "reason": raw["reason"], "basis": raw["basis"]}
    expected = {"status", "source", "value", "source_frame_sha256", "hypothetical_spend"}
    if status != "machine_candidate" or set(raw) != expected:
        _fail("candidate_schema_invalid")
    if (raw["source"] != "video-economy-candidate-v1"
            or raw["source_frame_sha256"] != frame_sha
            or type(raw["value"]) is not int or not 0 <= raw["value"] <= 999
            or type(raw["hypothetical_spend"]) is not int
            or not 0 <= raw["hypothetical_spend"] <= raw["value"]):
        _fail("machine_candidate_invalid")
    return {"state": "conditional_explanation_visible", "candidate": raw["value"],
            "source": raw["source"], "spend": raw["hypothetical_spend"],
            "balance_after": raw["value"] - raw["hypothetical_spend"], "reason": None,
            "basis": "same_frame_machine_candidate"}


def _validate(manifest: dict) -> list[dict]:
    if set(manifest) != {"schema_version", "purpose", "source_kind", "frames"}:
        _fail("manifest_schema_invalid")
    if (manifest["schema_version"] != SCHEMA
            or manifest["purpose"] != "private_replay_condition_demo"
            or manifest["source_kind"] != "recovered_recording_development"):
        _fail("manifest_scope_invalid")
    frames = manifest["frames"]
    if type(frames) is not list or not 2 <= len(frames) <= 12:
        _fail("frame_count_invalid")
    output, prior_time, prior_sha, saw_candidate = [], None, None, False
    expected_keys = {"id", "requested_time_s", "decoded_pts_s", "path", "sha256", "candidate"}
    for sequence, raw in enumerate(frames, 1):
        if type(raw) is not dict or set(raw) != expected_keys:
            _fail("frame_schema_invalid")
        if type(raw["id"]) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", raw["id"]):
            _fail("frame_id_invalid")
        requested = _finite_time(raw["requested_time_s"])
        decoded = None if raw["decoded_pts_s"] is None else _finite_time(raw["decoded_pts_s"])
        if (not 0 <= requested <= 86_400 or decoded is not None and not 0 <= decoded <= 86_400
                or prior_time is not None and not 0 < requested - prior_time <= 5.0):
            _fail("frame_time_order_or_gap_invalid")
        image, frame_sha = _bound_png(raw["path"], raw["sha256"])
        if frame_sha == prior_sha:
            _fail("repeated_frame_invalid")
        candidate = _candidate(raw["candidate"], frame_sha)
        saw_candidate = saw_candidate or candidate["state"] == "conditional_explanation_visible"
        output.append({"id": raw["id"], "sequence": sequence, "requested_time_s": requested,
                       "decoded_pts_s": decoded, "source_sha256": frame_sha,
                       "image": image, **candidate})
        prior_time, prior_sha = requested, frame_sha
    if not saw_candidate or output[-1]["state"] != "withdrawn_unknown":
        _fail("candidate_then_withdrawal_required")
    return output


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _frame_card(frame: dict) -> str:
    image = base64.b64encode(frame["image"]).decode("ascii")
    if frame["state"] == "conditional_explanation_visible":
        main = (f"机器金币候选：{frame['candidate']}；显式假设花费：{frame['spend']}；"
                f"算术结果：{frame['candidate']} − {frame['spend']} = {frame['balance_after']}。")
        detail = "这是同帧机器候选加显式假设，不代表已识别真实价格、主体、版本或买卖指令。"
        source = f"候选来源（调用方声明）：{frame['source']}（同帧图片字节摘要已绑定）"
    else:
        main = "本帧没有可消费的金币机器候选：条件说明已撤回。"
        detail = "不沿用上一帧数值、假设支出或余额；不产生行动。"
        source = (f"撤回原因：{frame['reason']}；本模块不运行 OCR，"
                  "本帧未提供新的可消费候选。")
    pts = "未核实" if frame["decoded_pts_s"] is None else f"调用方声明 {frame['decoded_pts_s']:.3f}s"
    return f"""<article class='{_esc(frame['state'])}' data-sequence='{frame['sequence']}' hidden>
<img alt='私有回放第 {frame['sequence']} 帧' src='data:image/png;base64,{image}'>
<div class='body'><p class='meta'>连续回放第 {frame['sequence']} 帧 · 请求 {frame['requested_time_s']:.3f}s · 解码 PTS {_esc(pts)}</p>
<h2>{'条件说明可见' if frame['state'] == 'conditional_explanation_visible' else '条件说明已撤回'}</h2>
<p class='main'>{_esc(main)}</p><p>{_esc(detail)}</p><p class='meta'>{_esc(source)}</p>
<details><summary>同帧绑定摘要</summary><code>{_esc(frame['source_sha256'])}</code></details></div></article>"""


def _page(frames: list[dict]) -> str:
    cards = "".join(_frame_card(frame) for frame in frames)
    return f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
<meta http-equiv='Content-Security-Policy' content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; connect-src 'none'; media-src 'none'">
<title>私有回放条件说明演示</title><style>
:root{{color-scheme:dark;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;background:#111821;color:#edf3fa}}body{{margin:auto;max-width:1180px;padding:28px;line-height:1.6}}.notice,.body{{background:#192534;border:1px solid #385069;border-radius:14px;padding:18px}}.notice{{border-color:#776534}}button{{color:#eef4fa;background:#27506f;border:1px solid #6398bf;border-radius:8px;padding:8px 13px;margin-right:8px;font:inherit}}button:disabled{{opacity:.4}}article{{margin-top:18px;display:grid;grid-template-columns:minmax(0,1.45fr) minmax(280px,1fr);gap:18px}}article[hidden]{{display:none !important}}article img{{width:100%;border-radius:12px;background:#000}}article.withdrawn_unknown .body{{border-color:#bf8b49}}h1,h2{{margin-top:0}}.meta{{color:#b8c7d8;font-size:14px}}.main{{font-size:18px;color:#ffe0a1}}code{{font-size:11px;overflow-wrap:anywhere}}@media(max-width:800px){{body{{padding:16px}}article{{grid-template-columns:1fr}}}}</style>
<main><h1>私有回放：同帧条件说明与撤回</h1><div class='notice'>这是离线开发演示。页面逐帧展示既有机器候选与显式算术假设；它不识别真实价格、棋子归属、版本或可执行买卖，不连接平板，也不构成实战建议。</div>
<p><button id='previous'>上一帧</button><button id='next'>下一帧</button><span id='position'></span></p>{cards}
<p class='meta'>这是调用方声明的私有抽取静帧；本模块不认证录像归属、连续性、PTS 或上游候选来源。未标注、未独立复核；机器候选和未知均不代表真实准确率。手动切换到未知帧时，先前条件说明被隐藏。</p></main>
<script>(()=>{{const cards=[...document.querySelectorAll('article')],position=document.querySelector('#position'),previous=document.querySelector('#previous'),next=document.querySelector('#next');let index=0;function show(){{cards.forEach((card,i)=>card.hidden=i!==index);position.textContent=`第 ${{index+1}} / ${{cards.length}} 帧`;previous.disabled=index===0;next.disabled=index===cards.length-1}}previous.onclick=()=>{{if(index){{index--;show()}}}};next.onclick=()=>{{if(index+1<cards.length){{index++;show()}}}};show()}})();</script></html>"""


def _output_directory(path: Path) -> Path:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        _fail("output_path_invalid")
    parent = _private_path(path.parent, directory=True)
    result = parent / path.name
    if result.exists() or result.is_symlink():
        _fail("output_path_invalid")
    try:
        result.mkdir(mode=0o700)
    except OSError:
        _fail("output_create_failed")
    return result


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(data)


def build_replay_coach(manifest_path: str | Path, output_directory: str | Path) -> dict:
    """Render a private short replay page from pre-bound still images."""
    frames = _validate(_load_manifest(Path(manifest_path)))
    output = _output_directory(Path(output_directory))
    report = {"schema_version": SCHEMA, "scope": "private_replay_condition_demo",
              "source_kind": "recovered_recording_development", "human_ground_truth_count": 0,
              "independent_review_count": 0, "accuracy": None, "advice_eligible": False,
              "strategy_recommendation": False, "automatic_action": None,
              "frames": [{key: frame[key] for key in ("id", "sequence", "requested_time_s", "decoded_pts_s", "source_sha256", "state", "candidate", "source", "spend", "balance_after", "reason", "basis")} for frame in frames]}
    _write_new(output / "report.html", _page(frames).encode("utf-8"))
    _write_new(output / "review.private.json", json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))
    return report
