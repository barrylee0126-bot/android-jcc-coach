"""Script-free, self-contained private HTML. All untrusted text is escaped."""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json

from PIL import Image

from .gold import METHOD, THRESHOLDS, check_gold_glyph
from .integrations import economy_comparison


FIELD_NAMES = {"round": "回合", "hp": "血量", "gold": "金币", "level": "等级"}
REASONS = {
    "variant_disagreement": "两种倍率的数字候选不同；保留冲突，不选值。",
    "variant_incomplete": "至少一种倍率没有合法候选。",
    "frame_witness_missing": "缺少本帧独立复核的上下文、可见性及主体证据。",
    "frame_expired": "超过离线门控的工程年龄参数；历史画面仍无实况新鲜度。",
    "explicit_phase_cue_missing": "没有明确阶段线索；商店可见不能证明处于准备阶段。",
    "phase_cue_conflict": "同帧出现互相冲突的阶段线索。",
    "battle_cues_incomplete": "场景线索不足。",
    "battle_scene_required": "没有形成 battle 场景候选。",
    "blocking_cue_present": "检测到阻断界面线索。",
    "no_text": "OCR 没有返回文字。",
    "ambiguous_ocr_items": "OCR 返回多个文字框，无法确定单一数字。",
    "invalid_numeric_syntax_or_bounds": "文字不满足严格数字语法或显式边界。",
    "text_touches_crop_edge": "文字接触裁切边缘，可能被截断。",
    "low_raw_score": "低于未校准的原始 OCR 分数过滤参数。",
    "ocr_failed": "OCR 请求失败；异常原文未记录。",
    "unmatched_text": "文字未匹配显式线索规则。",
    "invalid_ocr_response": "OCR 返回格式异常。",
    "gold_glyph_disagreement": "6/9 候选与本帧像素中的环孔位置相反；金币未知，阻断经济比较。",
    "gold_glyph_inconclusive": "无法稳定提取并核对 6/9 字形；金币未知，阻断经济比较。",
    "gold_roi_boundary_foreground": "金币输入框边界存在亮色像素，无法区分截断笔画与图标；金币未知，阻断经济比较。",
}

CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
       "script-src 'none'; connect-src 'none'; font-src 'none'; media-src 'none'; "
       "object-src 'none'; frame-src 'none'; worker-src 'none'; "
       "base-uri 'none'; form-action 'none'")

STYLE = """
:root { color-scheme:dark; font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;
  color:#edf2f9; background:#111722; line-height:1.65; font-size:16px; }
* { box-sizing:border-box; } body { margin:0; } a { color:#9acaff; }
main { max-width:1580px; margin:auto; padding:32px; } h1 { font-size:30px; margin:8px 0; }
h2 { margin:0 0 12px; font-size:23px; } h3 { font-size:17px; margin:12px 0 6px; }
p { margin:8px 0; } .muted { color:#adb9ca; } .eyebrow { color:#91e2c4; letter-spacing:2px; }
.notice { background:#302818; border:1px solid #736039; border-radius:12px; padding:15px 20px; }
.tag { display:inline-block; padding:3px 10px; border:1px solid #455568; border-radius:18px;
  margin:0 5px 6px 0; font-size:13px; color:#cbd8e6; }
.candidate { color:#9bd9ff; } .conflict { color:#ffc67a; } .unknown { color:#bdc6d2; }
.metrics { display:flex; flex-wrap:wrap; gap:14px; margin:20px 0; }
.metric { background:#1b2534; border-radius:12px; padding:12px 22px; min-width:148px; }
.metric strong { font-size:27px; display:block; } .metric span { color:#adb9ca; font-size:14px; }
.panel { background:#182130; border:1px solid #303f52; border-radius:15px; padding:22px; margin:24px 0; }
table { border-collapse:collapse; width:100%; } th,td { text-align:left; padding:10px 12px;
  border-bottom:1px solid #344154; vertical-align:top; } th { color:#adb9ca; font-weight:500; }
.table-wrap { overflow:auto; } .overview td { white-space:nowrap; } .value { font-size:25px; font-weight:650; }
.frame-layout { display:grid; grid-template-columns:minmax(0,1.6fr) minmax(320px,1fr); gap:22px; }
.image-box { position:relative; width:100%; line-height:0; border-radius:10px; overflow:hidden;
  background:#080d14; align-self:start; }
.image-box img { display:block; width:100%; height:auto; }
.roi { position:absolute; border:2px solid #93ceff; background:#162f4a30; }
.roi span { position:absolute; top:100%; left:0; color:#06182c; background:#a5d8ff;
  font-size:11px; white-space:nowrap; padding:8px 5px; line-height:0; }
.roi.gold { border-color:#ffcc80; } .roi.gold span { background:#ffcc80; }
.small { font-size:13px; } .field-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.field { padding:12px; background:#111b29; border-radius:10px; border:1px solid #314257; }
.field.conflict { border-color:#916a37; } .field .name { color:#bdcadb; font-size:14px; }
.field .note { font-size:12px; line-height:1.5; margin:6px 0 0; }
details { margin:14px 0; } summary { cursor:pointer; color:#a5d8ff; padding:8px 0; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; background:#0e1723; padding:12px;
  border-radius:8px; color:#c9d5e6; font-size:12px; max-height:420px; overflow:auto; }
code { overflow-wrap:anywhere; } .frame-head { display:flex; justify-content:space-between; gap:15px; flex-wrap:wrap; }
footer { color:#9baabc; padding:4px 0 30px; font-size:13px; }
.glyph-images { display:flex; flex-wrap:wrap; gap:12px; }
.glyph-images figure { margin:0; } .glyph-images img { height:180px; width:auto; image-rendering:pixelated; }
.glyph-images figcaption { color:#adb9ca; font-size:12px; }
@media(max-width:1050px) { .frame-layout { grid-template-columns:1fr; } main { padding:18px; } }
@media(max-width:550px) { .field-grid { grid-template-columns:1fr; } .panel { padding:15px; } h1 { font-size:25px; } }
"""


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def value_text(value) -> str:
    if value is None:
        return "未知"
    if isinstance(value, dict):
        return f"{value['stage']}-{value['step']}"
    return str(value)


def reason_text(reason) -> str:
    if reason is None:
        return "两倍率一致，仅为数字语法候选；可能共同读错。"
    return REASONS.get(reason, "保守返回未知；原因码见详情。")


def _field_card(probe: dict) -> str:
    conflict = probe["reason"] == "variant_disagreement"
    status = "conflict" if conflict else "candidate" if probe["status"] == "development_candidate" else "unknown"
    label = "冲突 · 不选值" if conflict else value_text(probe["candidate"])
    return (f'<div class="field {status}" data-field="{esc(probe["field"])}" data-status="{esc(probe["status"])}">'
            f'<div class="name">{FIELD_NAMES[probe["field"]]} · 机器候选</div>'
            f'<div class="value">{esc(label)}</div><p class="note">{esc(reason_text(probe["reason"]))}</p></div>')


def _variant_table(frame: dict) -> str:
    rows = []
    for name in FIELD_NAMES:
        for variant in frame["variants"]:
            probe = next(p for p in variant["fields"] if p["field"] == name)
            rows.append(f'<tr><td>{FIELD_NAMES[name]}</td><td>{variant["scale"]}×</td>'
                        f'<td>{esc(value_text(probe["candidate"]))}</td><td>{esc(probe["status"])}</td>'
                        f'<td>{esc(probe["reason"] or "candidate_only")}</td></tr>')
    return ('<details><summary>查看两倍率候选及未知原因</summary><div class="table-wrap"><table>'
            '<thead><tr><th>字段</th><th>倍率</th><th>语法候选</th><th>状态</th><th>原因码</th></tr></thead>'
            '<tbody>' + ''.join(rows) + '</tbody></table></div>'
            '<p class="muted small">倍率来自同一 OCR，属于相关方法；一致不等于独立复核。原始分数不是准确率。</p></details>')


def _raw_details(frame: dict) -> str:
    rows = []
    for request in frame["raw_ocr"]:
        raw = json.dumps(request.get("items"), ensure_ascii=False, indent=2, allow_nan=False)
        rows.append(f'<details><summary>请求 {request["request"]} · {esc(request["group"])} · '
                    f'{request["processing_ms"]:.1f} ms · {esc(request.get("error", "已返回"))}</summary>'
                    f'<pre>{esc(raw)}</pre></details>')
    return ('<details class="raw-ocr"><summary>原始 OCR 返回内容（仅本机，可展开）</summary>'
            '<p class="muted small">按真实调用顺序记录；未拼接、修字或根据高分挑选。仅含实际局部输入的返回，未做整屏 OCR。</p>'
            + ''.join(rows) + '</details>')


def _economy_html(frame: dict) -> str:
    economy = frame["economy"]
    analysis = economy["result"]
    heading = '<h3>金币条件比较 · 规则未绑定</h3>'
    premise = ('<p class="muted small">仅把本帧无冲突、且未被 6/9 字形检查否决的金币开发候选映射为条件输入，未确认属于己方。'
               '价格 1 / 2 / 3 / 4 / 5 为五个独立假设，未识别商店价格。'
               '10 金一档、每档 1 金、封顶 5 金采用历史假设；本局模式、版本、特殊效果及计息时点均未绑定。'
               '假定计息前除这次扣款外余额不变，不预测下轮总金币，不产生买卖建议或执行动作。</p>')
    if economy["input_mapping"]["to_status"] != "candidate" or analysis["status"] == "blocked":
        cause = "金币存在倍率冲突" if economy["input_mapping"]["to_status"] == "conflict" else "金币未知或不满足候选输入条件"
        return '<section class="economy blocked">' + heading + premise + f'<p class="conflict">{cause}，暂停扣款及利息计算；不借用其他帧。</p></section>'
    rows = []
    for row in analysis["comparisons"]:
        before = row["interest_before"]["interest_gold"] if row["interest_before"] else None
        after = row["interest_after"]["interest_gold"] if row["interest_after"] else None
        interest = f'{before} → {after}' if before is not None and after is not None else "未知"
        delta = row["interest_delta"]
        change = f"少 {-delta}" if delta is not None and delta < 0 else "不变" if delta == 0 else "未知"
        status = ("仅条件计算" if row["status"] == "conditional" else
                  "假设价格超过候选余额，未计算" if row["status"] == "insufficient_candidate_gold" else "暂停计算")
        rows.append(f'<tr class="economy-row" data-status="{esc(row["status"])}"><td>{row["hypothetical_price"]}</td>'
                    f'<td>{esc(value_text(row["gold_after_hypothetical_purchase"]))}</td>'
                    f'<td>{esc(interest)}</td><td>{esc(change)}</td><td>{status}</td></tr>')
    return ('<section class="economy conditional">' + heading + premise +
            '<div class="table-wrap"><table><thead><tr><th>假设价格</th><th>假设扣款后余额</th><th>假设利息前→后</th><th>变化</th><th>状态</th></tr></thead>'
            '<tbody>' + ''.join(rows) + '</tbody></table></div></section>')


def _gold_glyph_html(frame: dict, image) -> str:
    check = frame["gold_glyph_check"]
    if not check["required"]:
        note = ("倍率缺失或冲突，未用字形覆盖原结果。" if check["status"] == "not_checked"
                else "候选不含 6/9，未做环孔核对；仍仅为双倍率数字语法候选。")
        return '<p class="muted small">金币字形检查：' + note + '</p>'
    labels = {"upper": "上半部", "lower": "下半部", "unresolved": "未能判定"}
    figures, rows = [], []
    with Image.open(io.BytesIO(image.data)) as original, original.crop(check["input_roi"]) as crop:
        for threshold in (None, *THRESHOLDS):
            picture = crop.copy() if threshold is None else crop.convert("L").point(
                lambda pixel: 255 if pixel >= threshold else 0)
            with picture, io.BytesIO() as out:
                picture.save(out, format="PNG")
                data = base64.b64encode(out.getvalue()).decode("ascii")
            caption = "同帧原始局部" if threshold is None else f"亮度分割 {threshold}"
            figures.append(f'<figure><img src="data:image/png;base64,{data}" alt="金币{caption}" loading="lazy">'
                           f'<figcaption>{caption}</figcaption></figure>')
    for measurement in check["measurements"]:
        glyphs = measurement["components"]
        positions = ' / '.join(labels[g["loop_position"]] for g in glyphs) or "未提取到内部字形"
        centers = ' / '.join(f'{h["center_y"]:.3f}' for g in glyphs for h in g["holes"]) or "无有效环孔"
        rows.append(f'<tr><td>{measurement["threshold"]}</td><td>{esc(positions)}</td><td>{esc(centers)}</td>'
                    f'<td>{measurement["boundary_foreground_pixels"]}</td></tr>')
    outcome = ("边界未检出亮色像素且环孔位置与候选相容；仍是未经独立验收的开发候选。" if check["status"] == "consistent"
               else reason_text(check["reason"]))
    return ('<section class="gold-glyph"><h3>金币 6/9 字形核对 · 开发检查</h3>'
            f'<p>{esc(outcome)}</p><div class="glyph-images">' + ''.join(figures) + '</div>'
            '<div class="table-wrap"><table><thead><tr><th>分割亮度</th><th>环孔位置</th><th>环孔中心 / 字形高度</th>'
            '<th>边界亮色像素</th></tr></thead>'
            '<tbody>' + ''.join(rows) + '</tbody></table></div>'
            '<p class="muted small">使用同一原图和金币输入框，直接测量连通字形中的环孔；不使用 OCR 分数或文字框。'
            '四档分割是同一图像方法的稳定性检查，不能算四份独立真值。'
            '任一档检出边界亮色像素即保守未知；无触边信号不证明数字完整。'
            '检查只会保留或否决原候选，不会将 6 与 9 互相改写；原倍率冲突继续未知。</p>'
            f'<details><summary>本帧字形证据与绑定 · {METHOD}</summary>'
            f'<pre>{esc(json.dumps(check, ensure_ascii=False, indent=2))}</pre></details></section>')


def _capacity_html(frame: dict) -> str:
    result = frame["capacity"]
    candidate = result["candidate"]
    display = (f'已用容量 {candidate["displayed_deployed"]} / 显示容量 {candidate["capacity"]} · '
               f'空位候选 {candidate["empty_slots"]}' if candidate is not None else "未知")
    reasons = {"occluded": "容量区域存在遮挡", "animation_unknown": "动画状态未知",
               "variant_disagreement": "倍率结果冲突", "variant_incomplete": "倍率结果不完整"}
    reason = '；'.join(reasons.get(r, r) for r in result["reason_codes"])
    return ('<section class="capacity" data-status="' + esc(result["status"]) + '">'
            '<h3>上场容量 · 历史画面机器候选</h3><p class="candidate">' + esc(display) + '</p>'
            '<p class="muted small">' + esc(result["reminder_zh"]) + '</p>'
            + (f'<p class="unknown small">未知原因：{esc(reason)}</p>' if reason else '')
            + '<p class="muted small">显示已用容量并非检测出的实际棋子数。原图字节精确匹配后才接入缓存结果；'
              '本次未重跑容量 OCR。阶段、主体、可上场棋子和本局规则均未绑定；提示不产生任何游戏操作。</p></section>')


def _shop_html(frame: dict) -> str:
    result = frame["shop"]
    slot_labels = {"candidate": "整格候选", "unknown": "整格未定", "empty_candidate": "空槽候选",
                   "occluded_candidate": "遮挡候选", "conflict": "冲突"}
    field_labels = {"candidate": "候选", "unknown": "未知", "conflict": "冲突"}
    rows = []
    for slot in result["slots"]:
        names = " / ".join(slot["names"]) if slot["names"] else "未知"
        rows.append(f'<tr class="shop-slot" data-slot-status="{esc(slot["status"])}" data-name-status="{esc(slot["name_status"])}" '
                    f'data-price-status="{esc(slot["price_status"])}"><td>{slot["slot"]}</td><td>{esc(names)}</td>'
                    f'<td>{slot_labels[slot["status"]]}</td><td>{field_labels[slot["price_status"]]}</td></tr>')
    error = f'<p class="unknown small">{esc(result["reason"])}</p>' if result["reason"] else ''
    return ('<section class="shop"><h3>商店 · 历史画面机器候选</h3>' + error +
            '<div class="table-wrap"><table><thead><tr><th>槽位</th><th>名称机器候选</th><th>槽位状态</th><th>费用识别状态</th></tr></thead>'
            '<tbody>' + ''.join(rows) + '</tbody></table></div>'
            '<p class="muted small">名称使用第三方候选词典，身份及版本未核实；空槽与遮挡仅为启发式候选。'
            '费用只显示识别状态，不用参考价格补值，不参与本页金币假设计算，不产生任何自动操作。</p></section>')


def _validate_render_bindings(frames, images, profile) -> None:
    """Last consumer check: reject missing, reordered, duplicate or crossed data."""
    if not frames or len(frames) != len(images):
        raise ValueError("replay_render_binding_mismatch")
    hashes, refs = set(), set()
    for frame, image in zip(frames, images, strict=True):
        source_hash, reference = frame.get("source_sha256"), frame.get("frame_ref")
        if (not isinstance(source_hash, str) or len(source_hash) != 64
                or not isinstance(reference, str) or not reference
                or source_hash in hashes or reference in refs
                or hashlib.sha256(image.data).hexdigest() != source_hash
                or frame.get("order") != image.order):
            raise ValueError("replay_render_binding_mismatch")
        hashes.add(source_hash)
        refs.add(reference)
        for name in ("economy", "capacity", "shop", "gold_glyph_check"):
            result = frame.get(name)
            if (not isinstance(result, dict) or result.get("input_source_sha256") != source_hash
                    or result.get("input_frame_ref") != reference):
                raise ValueError("replay_render_binding_mismatch")
        gold_roi = next(p.roi.box for p in profile.number_layouts[0].fields if p.field_name == "gold")
        if frame["gold_glyph_check"].get("input_roi") != list(gold_roi):
            raise ValueError("replay_render_binding_mismatch")
        raw_gold = [p for v in frame["variants"] for p in v["fields"] if p["field"] == "gold"]
        proposed = (raw_gold[0]["candidate"] if len(raw_gold) == 2
                    and all(p["status"] == "development_candidate" and p["reason"] is None
                            and type(p["candidate"]) is int
                            and p["candidate"] == raw_gold[0]["candidate"] for p in raw_gold) else None)
        expected = check_gold_glyph(image.data, roi=gold_roi, frame_ref=reference, proposed_value=proposed)
        if frame["gold_glyph_check"] != expected:
            raise ValueError("replay_render_gold_evidence_mismatch")
        if frame["economy"] != economy_comparison(frame):
            raise ValueError("replay_render_economy_mismatch")
        capacity = frame["capacity"]
        if (capacity.get("matched_source_sha256") not in (None, source_hash)
                or capacity.get("status") == "development_candidate"
                and capacity.get("matched_source_sha256") != source_hash):
            raise ValueError("replay_render_binding_mismatch")
        shop = frame["shop"]
        if (shop.get("matched_source_sha256") not in (None, source_hash)
                or shop.get("status") == "development_candidate"
                and shop.get("matched_source_sha256") != source_hash):
            raise ValueError("replay_render_binding_mismatch")


def _frame_html(frame: dict, image, profile) -> str:
    width, height = profile.key.geometry.image_size
    data = base64.b64encode(image.data).decode("ascii")
    mime = "image/png" if image.image_format == "PNG" else "image/jpeg"
    boxes = []
    for field in profile.number_layouts[0].fields:
        left, top, right, bottom = field.roi.box
        style = (f"left:{left / width * 100:.6f}%;top:{top / height * 100:.6f}%;"
                 f"width:{(right-left) / width * 100:.6f}%;height:{(bottom-top) / height * 100:.6f}%")
        boxes.append(f'<div class="roi {esc(field.field_name)}" style="{style}"><span>{FIELD_NAMES[field.field_name]}</span></div>')
    scene = frame["scene"]["scene"]
    scene_label = "对局画面（battle）开发候选" if scene["candidate"] == "battle" else "未知"
    phase = frame["scene"]["phase"]
    phase_label = "未知" if phase["candidate"] is None else f"{phase['candidate']} · 机器候选"
    ms = frame["timing_ms"]
    field_cards = ''.join(_field_card(p) for p in frame["fields"])
    gate_reasons = ', '.join(dict.fromkeys(p["reason"] or "unknown" for p in frame["semantic_gate"]))
    timing = (f'场景 {ms["scene"]:.1f} ms；数字 1× {ms["numeric_variants"][0]:.1f} ms / '
              f'2× {ms["numeric_variants"][1]:.1f} ms；对照 {ms["reconcile"]:.2f} ms；'
              f'金币字形 {ms["gold_glyph_check"]:.2f} ms；'
              f'门控 {ms["semantic_gate"]:.2f} ms。读取 {ms["asset_read"]:.1f} ms，'
              f'图像完整性检查 {ms["image_validation"]:.1f} ms。')
    return f'''<article class="panel replay-frame" id="frame-{frame['order']}">
      <div class="frame-head"><h2>历史帧 {frame['order']:02d}</h2><a href="#overview">返回候选概览 ↑</a></div>
      <p class="muted small">匿名引用：<code>{esc(frame['frame_ref'])}</code> · 仅在本机私有配置中稳定，不证明来源独立。</p>
      <div class="frame-layout"><div><div class="image-box">
        <img src="data:{mime};base64,{data}" width="{width}" height="{height}" alt="历史回放原始画面，第 {frame['order']} 帧" loading="lazy">
        {''.join(boxes)}</div>
        <p class="muted small">原始画面 {width} × {height}；框线只表示本机开发配置的数字 OCR 输入区域。画面中的账号等内容未脱敏，请勿公开此文件。</p>
      </div><div><div class="field-grid">{field_cards}</div>
        <h3>场景与未核实条件</h3><p class="candidate">场景：{esc(scene_label)}</p>
        <p>阶段：{esc(phase_label)}</p><p class="muted small">{esc(reason_text(phase['reason']))}</p>
        <p><span class="tag">主体未核实</span><span class="tag">遮挡未核实</span><span class="tag">模式未核实</span><span class="tag">版本未核实</span></p>
        <p class="unknown small">正式语义解释：4 项未知。<code>{esc(gate_reasons)}</code><br>
        {esc(reason_text(frame['semantic_gate'][0]['reason']))}</p>
        <p class="muted small">离线识别墙钟：{ms['offline_recognition_wall']:.1f} ms。{esc(timing)}</p>
        {_capacity_html(frame)}
      </div></div>
      {_shop_html(frame)}
      {_gold_glyph_html(frame, image)}
      {_economy_html(frame)}
      {_variant_table(frame)}
      <details><summary>场景线索与门控原因码</summary><pre>{esc(json.dumps({'scene': frame['scene'], 'semantic_gate': frame['semantic_gate']}, ensure_ascii=False, indent=2))}</pre></details>
      {_raw_details(frame)}
    </article>'''


def render_page(report: dict, images, profile) -> str:
    frames, summary = report["frames"], report["summary"]
    images = tuple(images)
    _validate_render_bindings(frames, images, profile)
    rows = []
    for frame in frames:
        values = []
        for p in frame["fields"]:
            text = "冲突 · 不选值" if p["reason"] == "variant_disagreement" else value_text(p["candidate"])
            cls = "conflict" if p["reason"] == "variant_disagreement" else "candidate" if p["candidate"] is not None else "unknown"
            values.append(f'<td class="{cls}">{esc(text)}</td>')
        rows.append(f'<tr><td><a href="#frame-{frame["order"]}">历史帧 {frame["order"]:02d} ↓</a></td>'
                    + ''.join(values) + f'<td>{frame["timing_ms"]["offline_recognition_wall"]:.1f} ms</td></tr>')
    frame_html = ''.join(_frame_html(f, i, profile) for f, i in zip(frames, images, strict=True))
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{esc(CSP)}">
<meta name="referrer" content="no-referrer"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>历史回放识别检查 · 本机私有</title><style>{STYLE}</style></head>
<body><main><div class="eyebrow">LOCAL REPLAY INSPECTOR</div>
<h1>历史回放识别检查</h1><p class="muted">把真实历史画面与机器候选放在一起，查看读到了什么、哪里仍未知。</p>
<div class="notice"><strong>历史回放 · 开发探针 · 机器候选 · 非实战建议</strong>
<p>这些图已用于开发调参，未形成独立人工真值或验收集。主体、阶段、模式及版本尚未核实；无准确率成绩。
离线重新读取不代表新鲜实况，以下耗时仅为本机历史图片处理，不是端到端实战时延。</p>
<p class="small">仅供本机查看，原图与原始 OCR 留在私有文件；未启动浏览器服务，无需网络。请勿上传或分享此报告。</p></div>
<div class="metrics"><div class="metric"><strong>{summary['frames']}</strong><span>历史开发画面</span></div>
<div class="metric"><strong>{summary['scene_candidates']}</strong><span>场景开发候选</span></div>
<div class="metric"><strong>{summary['numeric_candidates']}</strong><span>数字开发候选</span></div>
<div class="metric"><strong>{summary['variant_conflicts']}</strong><span>保留的数字冲突</span></div>
<div class="metric"><strong>{summary['semantic_unknown']}</strong><span>正式语义仍未知</span></div></div>
<section class="panel" id="overview"><h2>回放机器候选概览</h2><div class="table-wrap">
<table class="overview"><thead><tr><th>画面</th><th>回合候选</th><th>血量候选</th><th>金币候选</th><th>等级候选</th><th>离线识别墙钟</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div><p class="muted small">合法数字可能属于正在观察的玩家或其他界面元素；不能直接解释为己方局面。
冲突保留两种结果供查看，不按原始分数或倍率挑值。下方框线、局部区域及配置不适用于未经校准的分辨率或直播。</p></section>
{frame_html}<footer>报告生成于 {esc(report['generated_utc'])}（UTC；是报告生成时间，不是历史采集时间）。
OCR 共 {summary['ocr_requests']} 次。源文件字节核验未变，未写标签、复核或分集；正式识别、建议和设备动作均为 0。
关闭此页面即可结束查看；OCR worker 已在生成完成后关闭，无常驻服务。</footer></main></body></html>'''
