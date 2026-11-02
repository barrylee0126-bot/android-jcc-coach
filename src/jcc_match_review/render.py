"""Static view: reuse 17A escaping/CSP/styles; never start a web service."""

from __future__ import annotations

import base64
import io
import json

from PIL import Image

from jcc_replay_demo.render import CSP, STYLE, esc, reason_text
from .private import ReviewError, digest


EXTRA_STYLE = """
main {max-width:1660px} .review-grid{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(390px,1fr);gap:22px}
.evidence-table{font-size:14px}.evidence-table th{white-space:nowrap}.evidence-table td{overflow-wrap:anywhere}
.crop-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin:16px 0}
.crop{margin:0;background:#101824;padding:12px;border-radius:9px}.crop img{max-width:100%;height:auto;image-rendering:auto}
.crop figcaption{font-size:13px;color:#bdc6d2}.time{font-variant-numeric:tabular-nums;font-size:22px;color:#91e2c4}
.boundary{border-left:3px solid #ffc67a;padding:3px 12px;margin:12px 0}.codes{font-size:12px;color:#adb9ca;overflow-wrap:anywhere}
.period{color:#91e2c4}.window-nav{display:flex;gap:14px;flex-wrap:wrap}.frame-head{flex-wrap:wrap}
details pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}.review-image{display:block;width:100%;height:auto}
@media(max-width:1100px){.review-grid{grid-template-columns:1fr}main{padding:18px}}
@media print{details{display:block}.panel{break-inside:avoid}body{background:white;color:black}}
"""


def data_image(raw, fmt="PNG"):
    mime = "image/png" if fmt == "PNG" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def time_text(seconds):
    if seconds is None:
        return "未知"
    seconds = float(seconds)
    return f"{int(seconds // 60):02d}:{seconds % 60:06.3f}"


def value_text(value):
    if value is None:
        return "未知"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def validate_bindings(report, image_bytes):
    frames = report["frames"]
    ids = set()
    for frame in frames:
        key = frame["frame_id"]
        if key in ids or key not in image_bytes or digest(image_bytes[key]) != frame["source_sha256"]:
            raise ReviewError("render_source_binding_mismatch")
        if frame.get("video_sha256") != report.get("video_sha256"):
            raise ReviewError("render_video_binding_mismatch")
        ids.add(key)
        for record in (*frame["observations"], *frame["regions"]):
            if record.get("source_sha256") != frame["source_sha256"]:
                raise ReviewError("render_evidence_binding_mismatch")
        for row in frame["observations"]:
            if row["authority"] not in ("machine_candidate", "agent_visual", "missing"):
                raise ReviewError("render_authority_invalid")
            if row["status"] not in ("development_candidate", "unknown"):
                raise ReviewError("render_status_invalid")
            if row["status"] != "development_candidate" and row["candidate"] is not None:
                raise ReviewError("unknown_must_not_have_value")
            if row["authority"] == "missing" and row["candidate"] is not None:
                raise ReviewError("missing_must_not_have_value")
    if set(image_bytes) != ids:
        raise ReviewError("render_image_count_mismatch")
    if (report.get("human_ground_truth_count") != 0 or report.get("independent_review_count") != 0
            or report.get("accuracy", "missing") is not None or report.get("automatic_action", "missing") is not None
            or report.get("advice_eligible") is not False):
        raise ReviewError("render_authority_invalid")


def _rows(frame):
    authority = {"machine_candidate": "机器候选", "agent_visual": "Agent 目视", "missing": "缺失"}
    result = []
    for row in frame["observations"]:
        state = "candidate" if row["candidate"] is not None else "unknown"
        reasons = row["reason_codes"]
        explanation = row.get("explanation") or "；".join(reason_text(code) for code in reasons)
        result.append(f'<tr data-authority="{esc(row["authority"])}" data-status="{esc(row["status"])}">'
                      f'<td>{esc(row["label"])}</td><td>{authority[row["authority"]]}</td>'
                      f'<td class="{state}">{esc(value_text(row["candidate"]))}</td>'
                      f'<td>{esc(explanation)}<div class="codes">{esc(" · ".join(reasons))}</div></td></tr>')
    return "".join(result)


def _crops(frame, raw):
    result = []
    with Image.open(io.BytesIO(raw)) as original:
        for region in frame["regions"]:
            roi = region["roi"]
            if (not isinstance(roi, list) or len(roi) != 4 or any(type(v) is not int for v in roi)
                    or not 0 <= roi[0] < roi[2] <= original.width or not 0 <= roi[1] < roi[3] <= original.height):
                raise ReviewError("render_roi_invalid")
            with original.crop(roi) as crop:
                encoded = io.BytesIO()
                crop.save(encoded, format="PNG")
            result.append(f'<figure class="crop"><img src="{data_image(encoded.getvalue())}" '
                          f'alt="{esc(region["label"])}的原图局部"><figcaption>{esc(region["label"])} · '
                          f'{esc(region["selection"])}<br>整帧坐标 {esc(roi)}</figcaption></figure>')
    return "".join(result)


def _frame(frame, raw):
    stamp = frame["recording_time"]
    with Image.open(io.BytesIO(raw)) as image:
        width, height = image.size
        fmt = image.format
    if [width, height] != frame["image_size"]:
        raise ReviewError("render_geometry_mismatch")
    details = {k: frame[k] for k in ("source_sha256", "video_sha256", "recording_time", "provenance", "regions")}
    return f'''<article class="panel" id="{esc(frame['frame_id'])}">
<div class="frame-head"><h2>{esc(frame['title'])}</h2><span class="period">{esc(frame['period'])}</span></div>
<p class="time">录像 {time_text(stamp['requested_seconds'])}</p>
<p class="muted small">时间依据：{esc(stamp['basis'])}；解码帧 PTS：{esc(time_text(stamp.get('decoded_pts_seconds')))}。
报告生成时间不能用作录像时间。</p>
<div class="review-grid"><div><img class="review-image" src="{data_image(raw, fmt)}" width="{width}" height="{height}"
alt="{esc(frame['title'])}的本局源图" loading="lazy"><p class="small muted">{esc(frame.get('image_description', '本局源图'))}
{width} × {height}；含私人对局信息，请留本机。</p>
<div class="crop-grid">{_crops(frame, raw)}</div></div>
<div><div class="table-wrap"><table class="evidence-table"><thead><tr><th>观察项</th><th>来源</th><th>结果</th><th>依据／拒绝原因</th></tr></thead>
<tbody>{_rows(frame)}</tbody></table></div>
<div class="boundary"><strong>上下文仍未获独立认证</strong><p>阶段、棋盘归属、可操作性、版本规则均未知。
阶段横幅等 Agent 或机器线索可单列为开发候选，不能认证为己方准备阶段。</p></div>
<p>{esc(frame['note'])}</p><p class="muted small">Agent 目视不是人类真值；同一 OCR 的多倍率一致也不是独立复核。</p>
</div></div><details><summary>核对源帧、时间与输入区域摘要</summary><pre>{esc(json.dumps(details, ensure_ascii=False, indent=2))}</pre></details>
<p class="small"><a href="#overview">回到索引 ↑</a></p></article>'''


def render_page(report, image_bytes):
    validate_bindings(report, image_bytes)
    navigation = []
    for case in report["cases"]:
        links = " · ".join(f'<a href="#{esc(key)}">{esc(report["frame_titles"][key])}</a>' for key in case["frame_ids"])
        navigation.append(f'<tr><td>{esc(case["title"])}</td><td>{links}</td><td>{esc(case["limitation"])}</td></tr>')
    frames = "".join(_frame(frame, image_bytes[frame["frame_id"]]) for frame in report["frames"])
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{esc(CSP)}"><meta name="referrer" content="no-referrer">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>本局可复查回放 · 本机私有</title>
<style>{STYLE}{EXTRA_STYLE}</style></head><body><main><div class="eyebrow">金铲铲 · LOCAL MATCH REVIEW / 21A</div>
<h1>本局可复查回放</h1><p class="muted">在同一帧对照原画、录像时间、机器候选、Agent 目视和未知原因。</p>
<div class="notice"><strong>本机私有 · 开发回放 · 非人类真值 · 非实战建议</strong>
<p>图像含私人对局信息，未脱敏；仅在本机查看。材料来自同一局，手调 ROI 和多倍率不能增加独立样本数。
真人真值 0，独立人工复核 0，准确率未知。离线处理时延不是实战成绩，也未测设备到人眼或动作效果。</p>
<p class="small">本页无脚本、外链、联网资源、遥测或常驻服务。只读消费已固定材料；未知不以 0 或其他帧补值。</p></div>
<div class="metrics"><div class="metric"><strong>{len(report['frames'])}</strong><span>真实源帧</span></div>
<div class="metric"><strong>{len(report['cases'])}</strong><span>可复查案例／窗口</span></div>
<div class="metric"><strong>0</strong><span>人类真值与自动动作</span></div></div>
<section class="panel" id="overview"><h2>从这里查看</h2><div class="table-wrap"><table><thead><tr><th>案例</th><th>帧入口</th><th>当前限制</th></tr></thead>
<tbody>{''.join(navigation)}</tbody></table></div><p class="small muted">12/12 例保留首次拒绝和同帧扩大 ROI 的开发候选；不能当成盲验。</p></section>
{frames}<footer>生成时间 {esc(report['generated_utc'])}（UTC）；仅为生成时间。源材料摘要在生成前后均复核。
结构化结果及固定清单保存在同一私有输出目录。正式局面、实战建议和自动操作均未生成。</footer></main></body></html>'''
