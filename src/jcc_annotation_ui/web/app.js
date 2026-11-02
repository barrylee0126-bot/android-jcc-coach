"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const names = {scene:"场景", phase:"阶段", round:"回合", hp:"血量", gold:"金币", level:"等级"};
  const fields = Object.keys(names);
  const scenes = {lobby:"大厅", mode_select:"模式选择", battle:"对局画面", loading:"加载", settlement:"结算", history:"历史", options_dialog:"选项弹窗", other:"其他（已核实）"};
  const phases = {preparation:"准备", combat:"战斗", carousel:"选秀", selection:"选择", transition:"过渡"};
  const states = {undetermined:"是否适用也未知", unknown:"适用，但答案未知", known:"适用，答案已知", not_applicable:"确实不适用"};
  const reasons = {not_annotated:"尚未标注", occluded:"遮挡", unreadable:"不可读", animation:"动画", insufficient_evidence:"证据不足", layout_unsupported:"布局不支持"};
  const splitNames = {quarantine:"隔离区",development:"开发集",acceptance:"验收集（标签冻结）"};
  const warningNames = {review_binding_stale:"原复核不再匹配当前标签",known_region_required:"已知字段缺少证据区域",occluded_known_field:"已知字段证据与遮挡重叠"};
  let token = location.hash.slice(1);
  history.replaceState(null, "", "/");
  let queue = [], view = null, draft = null, image = null, imageURL = null;
  let dirty = false, busy = false, target = null, pointer = null, sequence = 0, stopped = false;
  let activity = "正在载入";
  let heartbeat = null;
  const canvas = $("image"), ctx = canvas.getContext("2d");
  function say(text, error = false) { $("message").textContent = text; $("message").classList.toggle("error", error); }
  function el(tag, text, className) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (className) e.className = className; return e; }
  function option(select, value, text) { const o = el("option", text); o.value = value; select.append(o); }
  function label(text, control) { const l = el("label", text); l.append(control); return l; }
  function select(options, selected) { const s = el("select"); for (const [value, text] of Object.entries(options)) option(s, value, text); s.value = selected; return s; }
  function input(value, placeholder = "") { const e = el("input"); e.value = value ?? ""; e.placeholder = placeholder; e.autocomplete = "off"; return e; }
  async function api(path, payload, extra = {}, raw = false) {
    const response = await fetch(path, {method: payload === undefined ? "GET" : "POST", cache:"no-store", credentials:"omit", redirect:"error",
      headers:{"X-JCC-Session":token, ...(payload === undefined ? {} : {"Content-Type":"application/json"}), ...extra},
      body:payload === undefined ? undefined : JSON.stringify(payload)});
    if (!response.ok) { let data; try { data = await response.json(); } catch { data = null; } throw new Error(data?.error?.message || "服务暂不可用，请重新打开专用会话。"); }
    return raw ? response.blob() : response.json();
  }
  function anchor() { return {sample_id:view.record.sample_id, sample_kind:view.sample_kind, expected_revision:view.revision, view_token:view.view_token}; }
  function showVerification(s) { if (s) $("verification-status").textContent = `本次字节已核对 ${s.byte_checked_assets}/${s.assets}；图片内容已核验 ${s.decoded_assets}/${s.assets}。保存前仍会重新检查；未查看图片可先完整核验。`; }
  function intersects(a, b) { return Math.max(a.x_min,b.x_min) < Math.min(a.x_max,b.x_max) && Math.max(a.y_min,b.y_min) < Math.min(a.y_max,b.y_max); }
  function errors() {
    if (!draft || !view) return [];
    const result = [];
    for (const [name, value] of Object.entries(draft.fields)) {
      if (value.truth_status !== "known") continue;
      if (value.value === null || value.value === "" || (name === "round" && (!Number.isSafeInteger(value.value?.stage) || !Number.isSafeInteger(value.value?.step) || value.value.stage < 1 || value.value.step < 1)) ||
          (["hp","gold","level"].includes(name) && !Number.isSafeInteger(value.value)) || (name === "level" && value.value < 1)) result.push(`${names[name]}：请明确填写有效答案`);
      if (!value.region) result.push(`${names[name]}：缺少证据区域`);
      else if (draft.occlusions.some(b => intersects(value.region, b))) result.push(`${names[name]}：证据区域与遮挡重叠`);
    }
    const readonly = [view.record.labels.mode, view.record.labels.game_version, ...view.record.labels.shop.map(s => s.content)];
    if (readonly.some(l => l.truth_status === "known" && l.region && draft.occlusions.some(b => intersects(l.region,b)))) result.push("只读字段与遮挡重叠，须用原工作流处理并保留未知");
    return result;
  }
  function controls() {
    const valid = !!view && !!image && !busy && !stopped;
    const issues = errors();
    $("region-errors").textContent = issues.join("；");
    $("save").disabled = !valid || !!issues.length || view?.record.split === "acceptance";
    $("review").disabled = !valid || dirty || !!issues.length || !$("review-confirm").checked;
    $("reload").disabled = busy || !view || stopped;
    $("dirty-status").textContent = busy ? activity : !view ? "未载入" : dirty ? "草稿未保存" : "当前保存版本";
    $("dirty-status").classList.toggle("dirty", dirty);
    $("full-region").disabled = !valid || !fields.includes(target) || view?.record.split === "acceptance";
    $("apply-region").disabled = !valid || !target || view?.record.split === "acceptance";
    $("occlusion-tool").disabled = !valid || view?.record.split === "acceptance";
    $("conditions").disabled = !valid || view?.record.split === "acceptance";
    $("fields").inert = !valid || view?.record.split === "acceptance";
    for (const id of ["annotator","reviewer","evidence","review-status","review-confirm"]) $(id).disabled = !valid;
    $("shutdown").disabled = busy || stopped;
    $("verify").disabled = busy || stopped || !view;
  }
  function changed() { dirty = true; $("review-confirm").checked = false; controls(); draw(); }
  function stateOf(value) { return value.truth_status === "known" ? "known" : value.applicability === "applicable" ? "unknown" : value.applicability; }
  function regionText(region) { return region ? [region.x_min,region.y_min,region.x_max,region.y_max].map(n => Number(n.toFixed(4))).join(", ") : "未设置；未假定默认区域"; }
  function renderFields() {
    $("fields").replaceChildren();
    for (const name of fields) {
      const value = draft.fields[name], card = el("div", undefined, "field-card");
      card.dataset.field = name; card.classList.toggle("active", target === name);
      const title = el("div", undefined, "field-title"); title.append(el("strong", names[name]));
      const choose = el("button", "选区"); choose.type = "button"; choose.disabled = view.record.split === "acceptance";
      choose.addEventListener("click", () => chooseTarget(name)); title.append(choose); card.append(title);
      const row = el("div", undefined, "field-controls"), status = select(states, stateOf(value)); status.dataset.role = "status";
      status.disabled = view.record.split === "acceptance";
      status.addEventListener("change", () => {
        const s = status.value;
        value.applicability = s === "known" || s === "unknown" ? "applicable" : s;
        value.truth_status = s === "known" ? "known" : "unknown";
        value.value = null; value.reason = s === "known" ? null : s === "not_applicable" ? "not_applicable" : "not_annotated";
        changed(); renderFields();
      });
      row.append(label("状态", status));
      const known = value.truth_status === "known", frozen = view.record.split === "acceptance";
      if (known) {
        let answer;
        if (name === "scene" || name === "phase") {
          answer = select({"":"请选择，不自动推断", ...(name === "scene" ? scenes : phases)}, value.value ?? "");
          answer.addEventListener("change", () => { value.value = answer.value || null; changed(); });
        } else if (name === "round") {
          answer = el("div", undefined, "round-inputs");
          for (const part of ["stage","step"]) {
            const n = input(value.value?.[part], part === "stage" ? "阶段" : "小回合"); n.type = "number"; n.min = "1"; n.step = "1"; n.dataset.part = part; n.disabled = frozen;
            n.addEventListener("input", () => { value.value = {...(value.value ?? {stage:null,step:null}), [part]: n.value === "" ? null : Number(n.value)}; changed(); });
            answer.append(n); if (part === "stage") answer.append(el("span", "−"));
          }
        } else {
          answer = input(value.value, "未填写，不默认 0"); answer.type = "number"; answer.step = "1"; if (name === "level") answer.min = "1";
          answer.addEventListener("input", () => { value.value = answer.value === "" ? null : Number(answer.value); changed(); });
        }
        answer.dataset.role = "value"; if (answer.tagName !== "DIV") answer.disabled = frozen;
        row.append(label("答案", answer));
      } else {
        const reasonOptions = {...reasons}; if (value.reason && !reasonOptions[value.reason]) reasonOptions[value.reason] = value.reason;
        const reason = select(reasonOptions, value.reason); reason.dataset.role = "reason";
        reason.disabled = frozen || value.applicability === "not_applicable";
        reason.addEventListener("change", () => { value.reason = reason.value; changed(); });
        row.append(label("未知原因", reason));
      }
      card.append(row);
      const region = el("div", "整帧区域：" + regionText(value.region), "field-region");
      const clear = el("button", "清除区域"); clear.type = "button"; clear.disabled = frozen || !value.region;
      clear.addEventListener("click", () => { value.region = null; changed(); renderFields(); syncCoordinates(); });
      region.append(clear); card.append(region); $("fields").append(card);
    }
  }
  function chooseTarget(name) { target = name; pointer = null; $("region-target").textContent = name === "occlusion" ? "当前工具：新增遮挡区域（不改变原图字节）" : "当前选区字段：" + names[name]; renderFields(); syncCoordinates(); controls(); draw(); }
  function syncCoordinates() { const b = draft && fields.includes(target) ? draft.fields[target].region : null; for (const k of ["x_min","y_min","x_max","y_max"]) $(k).value = b?.[k] ?? ""; }
  function setRegion(b) {
    if (!draft || !target || busy || view.record.split === "acceptance") return;
    const values = [b.x_min,b.y_min,b.x_max,b.y_max];
    if (values.some(n => !Number.isFinite(n) || n < 0 || n > 1) || b.x_min >= b.x_max || b.y_min >= b.y_max) { say("区域必须在整帧内且有正面积。", true); return; }
    if (target === "occlusion") { if (draft.occlusions.length >= 32) { say("最多可设置 32 个遮挡区域。", true); return; } draft.occlusions.push(b); }
    else draft.fields[target].region = b;
    changed(); renderFields(); renderOcclusions(); syncCoordinates();
  }
  function renderOcclusions() { $("occlusion-list").replaceChildren(); draft.occlusions.forEach((b,index) => { const button = el("button", `移除遮挡 ${index + 1} · ${regionText(b)}`); button.type = "button"; button.disabled = view.record.split === "acceptance"; button.addEventListener("click", () => { draft.occlusions.splice(index,1); changed(); renderOcclusions(); }); $("occlusion-list").append(button); }); }
  function drawBox(b, color, caption, fill = false) { const x = b.x_min*canvas.width, y = b.y_min*canvas.height, w = (b.x_max-b.x_min)*canvas.width, h = (b.y_max-b.y_min)*canvas.height; ctx.strokeStyle = color; ctx.lineWidth = target ? 3 : 2; if (fill) { ctx.fillStyle = "rgba(185,43,79,.23)"; ctx.fillRect(x,y,w,h); } ctx.strokeRect(x,y,w,h); if (caption) { ctx.font = "18px sans-serif"; const tw = ctx.measureText(caption).width + 12; const ty = y >= 24 ? y - 24 : y; ctx.fillStyle = "#122a3de8"; ctx.fillRect(x,ty,tw,24); ctx.fillStyle = color; ctx.fillText(caption,x+6,ty+18); } }
  function drawCrop() {
    const crop = $("crop"), b = draft && fields.includes(target) ? draft.fields[target].region : null;
    crop.hidden = !image || !b;
    if (!image || !b) { $("crop-caption").textContent = "选择字段并设置区域后可放大查看，不会推断或补全答案。"; return; }
    const x=Math.floor(b.x_min*canvas.width),y=Math.floor(b.y_min*canvas.height),right=Math.ceil(b.x_max*canvas.width),bottom=Math.ceil(b.y_max*canvas.height),w=right-x,h=bottom-y;
    crop.width=600;crop.height=Math.max(1,Math.min(360,Math.round(600*h/w)));
    const scale=Math.min(crop.width/w,crop.height/h),offsetX=(crop.width-w*scale)/2,offsetY=(crop.height-h*scale)/2;
    const c=crop.getContext("2d");c.drawImage(image,x,y,w,h,offsetX,offsetY,w*scale,h*scale);
    c.fillStyle="rgba(185,43,79,.23)";c.strokeStyle="#ff91ae";c.lineWidth=2;
    for(const covered of draft.occlusions) if(intersects(b,covered)) {
      const left=(Math.max(b.x_min,covered.x_min)*canvas.width-x)*scale+offsetX,top=(Math.max(b.y_min,covered.y_min)*canvas.height-y)*scale+offsetY;
      const width=(Math.min(b.x_max,covered.x_max)-Math.max(b.x_min,covered.x_min))*canvas.width*scale,height=(Math.min(b.y_max,covered.y_max)-Math.max(b.y_min,covered.y_min))*canvas.height*scale;
      c.fillRect(left,top,width,height);c.strokeRect(left,top,width,height);
    }
    $("crop-caption").textContent = `${names[target]} · 原图像素区域 [${x}, ${y}, ${right}, ${bottom})。仅放大，不改变标签或原图。`;
  }
  function draw() { ctx.clearRect(0,0,canvas.width,canvas.height); drawCrop(); if (!image) return; ctx.drawImage(image,0,0); for (const name of fields) { const l = draft.fields[name]; if (l.region) drawBox(l.region, name === target ? "#64ffd9" : "#8bd2e2", names[name]+(l.truth_status === "known" ? "·已知" : "·未知")); } for (const b of draft.occlusions) drawBox(b,"#ff91ae","遮挡",true); if (pointer?.end) drawBox({x_min:Math.min(pointer.start.x,pointer.end.x), y_min:Math.min(pointer.start.y,pointer.end.y),x_max:Math.max(pointer.start.x,pointer.end.x),y_max:Math.max(pointer.start.y,pointer.end.y)},"#fff3a0",""); }
  function point(event) { const r = canvas.getBoundingClientRect(); return {x:Math.max(0,Math.min(1,(event.clientX-r.left)/r.width)),y:Math.max(0,Math.min(1,(event.clientY-r.top)/r.height))}; }
  canvas.addEventListener("pointerdown", event => { if (!target || !image || busy || view?.record.split === "acceptance") { say("请先选择字段的“选区”或“绘制遮挡”。"); return; } pointer = {start:point(event),end:null}; canvas.setPointerCapture(event.pointerId); });
  canvas.addEventListener("pointermove", event => { if (pointer) { pointer.end = point(event); draw(); } });
  canvas.addEventListener("pointerup", event => { if (!pointer) return; const a = pointer.start, b = point(event); pointer = null; if (Math.abs(a.x-b.x)*canvas.width < 2 || Math.abs(a.y-b.y)*canvas.height < 2) { draw(); return; } setRegion({x_min:Math.min(a.x,b.x),y_min:Math.min(a.y,b.y),x_max:Math.max(a.x,b.x),y_max:Math.max(a.y,b.y)}); });
  canvas.addEventListener("pointercancel", () => { pointer = null; draw(); });
  function renderQueue() { $("sample-list").replaceChildren(); const query = $("filter").value.trim().toLowerCase(); for (const sample of queue.filter(s => (s.sample_id+" "+s.recording_session_id).includes(query))) { const b = el("button"); b.type = "button"; b.classList.toggle("active", view?.record.sample_id === sample.sample_id); b.append(el("span", sample.sample_id.slice(0,19)+"…", "sample-code")); b.append(el("span", `${sample.recording_session_id} · ${sample.known_fields}/6 已知`, "sample-sub")); b.append(el("span", sample.review_status === "draft" ? "未复核" : "已有复核记录", "sample-sub")); b.addEventListener("click", () => { if (busy || stopped) return; if (dirty) { say("当前有未保存草稿；请先保存，或点击“丢弃草稿并重载”。",true); return; } openSample(sample.sample_id).catch(e => say(e.message,true)); }); $("sample-list").append(b); } }
  async function loadQueue() { const data = await api("/api/samples"); queue = data.samples; $("sample-count").textContent = String(queue.length); const real = data.sample_kind === "real"; $("mode-banner").textContent = real ? "真实工作区 · 答案须由真实人员标注、另一位真实人员独立复核" : "合成工作区 · 仅验证软件流程，不代表真实人审、验收或识别成绩"; $("mode-banner").classList.toggle("real",real); $("review-title").textContent = real ? "独立复核当前已保存标签" : "合成复核流程测试"; $("confirmation-text").textContent = real ? "我确认本人是标注者以外的另一位真实人员，已检查当前图像、证据区域和保存标签；不同代号本身不能证明人员独立。" : "我只执行合成软件流程验证；这次操作不代表真实人员标注、独立复核或真实验收。"; renderQueue(); return data; }
  async function openSample(id) {
    const seq = ++sequence; busy = true; activity = "正在核对与载入"; image = null; draw(); controls();
    try {
      const incoming = await api("/api/sample/"+id);
      const blob = await api("/api/image/"+id, undefined, {"X-JCC-View":incoming.view_token,"X-JCC-Revision":String(incoming.revision),"X-JCC-Sample-Kind":incoming.sample_kind}, true);
      const url = URL.createObjectURL(blob), decoded = new Image();
      try { decoded.src = url; await decoded.decode(); } catch { URL.revokeObjectURL(url); throw new Error("浏览器不能解码当前图像，请检查私有样本。"); }
      if (seq !== sequence) { URL.revokeObjectURL(url); return; }
      if (imageURL) URL.revokeObjectURL(imageURL); imageURL = url; image = decoded; view = incoming;
      draft = {fields:Object.fromEntries(fields.map(k => [k,structuredClone(view.record.labels[k])])),conditions:structuredClone(view.conditions),occlusions:structuredClone(view.occlusions)};
      dirty = false; target = null; pointer = null; canvas.width = decoded.naturalWidth; canvas.height = decoded.naturalHeight;
      $("sample-title").textContent = id; $("sample-meta").textContent = `${view.record.recording_session_id} · 修订 ${view.revision} · ${canvas.width} × ${canvas.height} · ${splitNames[view.record.split]}`;
      $("conditions").value = draft.conditions.join("\n"); $("annotator").value = ""; $("reviewer").value = ""; $("evidence").value = ""; $("review-confirm").checked = false;
      $("region-target").textContent = "先选择字段的“选区”，或新增遮挡区域";
      const r = view.record;
      $("provenance").textContent = JSON.stringify({sample_id:id,dataset_id:r.dataset_id,sample_kind:r.sample_kind,annotation_method:r.annotation_method,split:r.split,recording_session_id:r.recording_session_id,dedupe_group_id:r.dedupe_group_id,state_group_id:r.state_group_id,frame_id:r.frame.frame_id,coordinate_space:r.frame.image.coordinate_space,image_sha256:view.image_sha256,labels_sha256:view.labels_sha256,annotator_id:r.annotator_id,reviewer_id:r.reviewer_id,review_status:r.review_status,review_binding:view.review_binding},null,2);
      $("readonly-labels").textContent = JSON.stringify({mode:r.labels.mode,game_version:r.labels.game_version,shop:r.labels.shop},null,2);
      $("review-state").textContent = r.review_status === "draft" ? "未复核 / 旧复核已撤销" : view.review_binding.current ? (view.sample_kind === "real" ? "当前标签已有复核记录" : "已有合成流程记录") : "复核与标签不匹配";
      $("review-instruction").textContent = `复核绑定当前图像、标签和工作区修订 ${view.revision}。标签摘要 ${view.labels_sha256.slice(0,16)}…；草稿有修改时不能复核。`;
      queue = queue.map(s => s.sample_id === id ? {...s,review_status:r.review_status,split:r.split,known_fields:fields.filter(k=>r.labels[k].truth_status==="known").length} : s);
      renderFields(); renderOcclusions(); renderQueue(); syncCoordinates(); draw(); showVerification(view.verification);
      if (view.warnings.length) say("当前保存版本有待处理约束："+view.warnings.map(w=>warningNames[w]||"画面条件需要检查").join("、")+"。请检查后再保存或复核。",true);
      else if (r.split === "acceptance") say("验收标签已冻结；本界面不改变分集。可检查当前保存版本。");
      else say("已载入当前图像和标签；所有变更先留在页面内，点击保存才写入。");
    } catch (error) { if (seq === sequence) say(error.message,true); throw error; }
    finally { if (seq === sequence) { busy = false; controls(); } }
  }
  $("filter").addEventListener("input",renderQueue);
  $("occlusion-tool").addEventListener("click", () => chooseTarget("occlusion"));
  $("full-region").addEventListener("click", () => setRegion({x_min:0,y_min:0,x_max:1,y_max:1}));
  $("apply-region").addEventListener("click", () => { const b = {}; for (const k of ["x_min","y_min","x_max","y_max"]) { if ($(k).value === "") { say("请完整填写四个区域坐标。",true); return; } b[k] = Number($(k).value); } setRegion(b); });
  $("conditions").addEventListener("input", () => { if (!draft) return; draft.conditions = $("conditions").value.split("\n").map(s=>s.trim()).filter(Boolean); changed(); });
  $("reload").addEventListener("click", () => { if (!view) return; dirty = false; const id=view.record.sample_id; loadQueue().then(()=>openSample(id)).catch(e=>say(e.message,true)); });
  $("review-confirm").addEventListener("change",controls);
  $("save").addEventListener("click", async () => { if (!view || busy || !image) return; busy = true; activity="提交前核验与保存"; controls(); say("正在核验整个工作区后保存；首次会检查尚未解码的图片，草稿仍保留在本页。"); const id=view.record.sample_id; try { const result = await api("/api/annotate",{...anchor(),...draft,annotator_id:$("annotator").value.trim()}); dirty = false; await openSample(id); say(`标注已保存到修订 ${result.revision}；旧复核已撤销，其他字段和分集保持原值。`); } catch(e) { say(e.message,true); } finally { busy = false; controls(); } });
  $("review").addEventListener("click", async () => { if (!view || busy || dirty || !image || !$("review-confirm").checked) return; busy = true; activity="提交前核验与复核"; controls(); say("正在重新核验整个工作区后记录复核，首次可能需要等待。"); const id=view.record.sample_id; try { const result=await api("/api/review",{...anchor(),reviewer_id:$("reviewer").value.trim(),evidence_ref:$("evidence").value.trim(),status:$("review-status").value,confirmation:view.sample_kind === "real" ? "different_human_checked" : "synthetic_software_check"}); await openSample(id); say(`已记录修订 ${result.revision} 的${view.sample_kind === "real" ? "复核" : "合成复核流程"}；未改变答案或分集。`); } catch(e) { say(e.message,true); } finally { busy = false; controls(); } });
  $("verify").addEventListener("click", async () => { if (!view || busy || stopped) return; busy=true; activity="正在完整核验图片"; controls(); say("正在完整核验图片；此操作不保存标注，不改变修订、复核或分集。"); try { const result=await api("/api/verify",{}); showVerification(result.verification); say(result.revision===view.revision ? "本次完整核验通过；草稿和样本状态未改变，提交时仍重新核对当前文件。" : "核验通过，但工作区修订已变化；草稿未保存，请重新载入并核对当前版本。",result.revision!==view.revision); } catch(e) { say(e.message,true); } finally { busy=false; controls(); } });
  $("shutdown").addEventListener("click", async () => { if (stopped) return; try { await api("/api/shutdown",{}); stopped=true; clearInterval(heartbeat); token=""; if (imageURL) URL.revokeObjectURL(imageURL); imageURL=null; image=null; ctx.clearRect(0,0,canvas.width,canvas.height); say("服务已关闭。未保存草稿未写入；再次使用须重新启动实例。"); controls(); } catch(e) { say(e.message,true); } });
  window.addEventListener("pagehide",()=>{ if (!stopped && token) fetch("/api/shutdown",{method:"POST",headers:{"X-JCC-Session":token,"Content-Type":"application/json"},body:"{}",keepalive:true,credentials:"omit",cache:"no-store",redirect:"error"}).catch(()=>{}); });
  if (!/^[A-Za-z0-9_-]{43}$/.test(token)) { token=""; stopped=true; say("请使用启动器提供的专用会话链接打开；普通地址或页面刷新后不能读取私有样本。",true); $("shutdown").disabled=true; controls(); return; }
  heartbeat=setInterval(()=>{ if (!stopped) api("/api/heartbeat",{}).catch(()=>{ stopped=true; clearInterval(heartbeat); say("本机服务已停止或连接中断。草稿未保存，请重新启动专用会话。",true); controls(); }); },25000);
  loadQueue().then(data=>data.samples.length ? openSample(data.samples[0].sample_id) : say("工作区没有样本。请先用现有 SampleStore 工作流导入。" )).catch(e=>say(e.message,true));
})();
