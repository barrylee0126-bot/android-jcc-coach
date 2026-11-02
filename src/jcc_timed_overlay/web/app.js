"use strict";
(() => {
  const byId = (id) => document.getElementById(id);
  const hint = byId("hint");
  const compact = new URLSearchParams(location.search).get("compact") === "1";
  if (compact) document.body.classList.add("compact");
  const clientId = "browser-" + crypto.randomUUID();
  const controls = [...document.querySelectorAll("[data-scenario]")];
  const copy = {
    unknown: ["等待事件", "等待模拟事件", "收到新的可靠时间字段后再显示。"],
    pending: ["等待结果", "模拟计时 · 等待结果", "新样本已到达，旧提示已撤下。"],
    expired: ["已过期", "模拟提示已到期", "显示内容已清空，等待新样本。"],
    timeout: ["已超时", "模拟计时已超时", "没有收到及时的结果或绘制机会回报。"],
    no_result: ["无结果", "模拟计时 · 没有结果", "本样本记入无结果，旧提示已撤下。"],
    invalidated: ["已撤回", "模拟提示已撤回", "源发出了失效事件，等待新样本。"],
    disconnected: ["源已断开", "模拟提示已撤下", "连接失败或来源静默，需要新的样本。"],
  };
  let session = null, revision = -1, retiredRevision = -1;
  let current = null, epoch = 0, inflight = null, busy = false;
  let pageActive = true;
  let clientEventSequence = 0, reportQueue = Promise.resolve();
  const put = (id, value) => { if (byId(id).textContent !== value) byId(id).textContent = value; };
  const idOK = (value) => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$/.test(value);
  const numberOK = (value, max) => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= max;

  function elapsed(mono, wall) {
    const m = performance.now() - mono, w = Date.now() - wall;
    return m < 0 || w < 0 ? Infinity : Math.max(m, w);
  }

  function report(stage, reason, shown, domToRaf = null) {
    if (!shown) return;
    const sinceFetch = elapsed(shown.fetchMono, shown.fetchWall);
    if (!Number.isFinite(sinceFetch)) return;
    const payload = {
      session_id: shown.session, client_id: clientId, client_event_sequence: ++clientEventSequence,
      revision: shown.revision, render_token: shown.token, sample_id: shown.sampleId,
      stage, reason, visibility: document.hidden ? "hidden" : "visible",
      browser_monotonic_ms: performance.now(), since_fetch_start_ms: sinceFetch,
      dom_to_raf_ms: domToRaf,
    };
    // Order this page's reports. A failed report is never silently counted as success.
    reportQueue = reportQueue.catch(() => {}).then(async () => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 800);
      try {
        await fetch("/api/browser-event", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload), signal: controller.signal, keepalive: true, cache: "no-store" });
      } finally { clearTimeout(timer); }
    }).catch(() => {});
  }

  function clear(state, detail) {
    const text = copy[state] || copy.unknown;
    hint.dataset.state = state in copy ? state : "unknown";
    put("state-label", text[0]); put("message", text[1]); put("detail", detail || text[2]);
    put("sample-id", "—");
    byId("countdown").hidden = true;
    byId("countdown").textContent = "";
    byId("lifetime").hidden = true;
    byId("lifetime").value = 0;
  }

  function withdraw(state, reason, detail) {
    const previous = current;
    current = null;
    retiredRevision = Math.max(retiredRevision, revision);
    clear(state, detail);
    report("withdrawn", reason, previous);
  }

  function invalidateViewRequests() {
    epoch += 1;
    if (inflight) inflight.abort();
    inflight = null;
  }

  function tick() {
    if (!current) return;
    const age = elapsed(current.startMono, current.startWall);
    if (!Number.isFinite(age)) { withdraw("unknown", "clock_unreliable"); return; }
    const remaining = current.remaining - age;
    if (remaining <= 0) { withdraw("expired", "expired"); return; }
    if (age >= current.lease) { withdraw("disconnected", "lease_elapsed"); return; }
    hint.dataset.state = remaining <= 1000 ? "expiring" : "ready";
    put("state-label", remaining <= 1000 ? "即将过期" : "测试显示中");
    put("countdown", `${(Math.floor(remaining / 100) / 10).toFixed(1)} 秒`);
    byId("lifetime").value = remaining;
  }

  function receive(data, fetchMono, fetchWall) {
    if (!data || data.schema_version !== "timed-overlay-view-v1" || data.simulation !== true || data.execution_eligible !== false || !idOK(data.session_id) || !Number.isSafeInteger(data.revision) || data.revision < 0) {
      withdraw("unknown", "unsafe_view"); return;
    }
    if (data.session_id !== session) {
      withdraw("unknown", "server_withdrawal");
      session = data.session_id; revision = -1; retiredRevision = -1;
    }
    if (data.revision < revision) return;
    const previous = current;
    revision = data.revision;
    byId("controls").hidden = data.input_mode !== "synthetic";
    put("source-label", ({ synthetic: "合成来源", replay: "历史回放来源 · 提示仍为模拟", device_readonly: "只读事件来源 · 提示仍为模拟" })[data.source_kind] || "来源未知");
    put("mode-note", data.playing ? "正在完整试播：包括失败与无结果，结束后不会自动循环。" : "一轮包含显示、过期、无结果、超时、撤回与源静默。");
    if (data.status !== "ready") {
      withdraw(data.status in copy ? data.status : "unknown", "server_withdrawal");
      if (data.status === "pending" && idOK(data.sample_id)) put("sample-id", data.sample_id);
      return;
    }
    if (!idOK(data.sample_id) || !idOK(data.render_token) || data.message !== "模拟计时 · 仅测试显示" || !numberOK(data.remaining_ms, 30000) || !numberOK(data.lease_ms, 1000) || data.lease_ms <= 0 || data.lease_ms > data.remaining_ms) {
      withdraw("unknown", "unsafe_view"); return;
    }
    if (!pageActive || document.hidden || revision <= retiredRevision) return;
    const age = elapsed(fetchMono, fetchWall);
    let remaining = data.remaining_ms - age;
    const same = previous && previous.revision === revision;
    if (same) {
      if (previous.token !== data.render_token || previous.sampleId !== data.sample_id) { withdraw("unknown", "unsafe_view"); return; }
      remaining = Math.min(remaining, previous.remaining - elapsed(previous.startMono, previous.startWall));
    }
    if (remaining <= 0) { withdraw("expired", "expired"); return; }
    const lease = data.lease_ms - age;
    if (!Number.isFinite(age) || lease <= 0) { withdraw("disconnected", "lease_elapsed"); return; }
    if (previous && !same) report("withdrawn", "superseded", previous);
    current = {
      session, revision, sampleId: data.sample_id, token: data.render_token,
      startMono: performance.now(), startWall: Date.now(), remaining, lease,
      fetchMono: same ? previous.fetchMono : fetchMono, fetchWall: same ? previous.fetchWall : fetchWall,
    };
    put("message", data.message); put("detail", "仅测试显示、计时与撤回，不执行游戏操作。");
    put("sample-id", data.sample_id);
    byId("countdown").hidden = false;
    byId("lifetime").hidden = false;
    byId("lifetime").max = Math.max(remaining, 700);
    tick();
    if (!same && current) {
      const shown = current;
      const domMono = performance.now(), domWall = Date.now();
      report("dom_updated", "none", shown);
      // A double rAF gives a rendering opportunity, not a pixel/human receipt.
      requestAnimationFrame(() => requestAnimationFrame(() => {
        tick();
        if (!current || current.token !== shown.token || document.hidden) return;
        const box = hint.getBoundingClientRect();
        if (box.width <= 0 || box.height <= 0 || box.bottom <= 0 || box.top >= innerHeight || box.right <= 0 || box.left >= innerWidth) return;
        const interval = elapsed(domMono, domWall);
        if (Number.isFinite(interval)) report("paint_opportunity", "none", shown, interval);
      }));
    }
  }

  async function poll() {
    if (inflight || !pageActive || document.hidden || busy) return;
    const token = epoch, controller = new AbortController();
    inflight = controller;
    const startMono = performance.now(), startWall = Date.now();
    const timeout = setTimeout(() => controller.abort(), 800);
    try {
      const response = await fetch("/api/view", { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error("unavailable");
      const data = await response.json();
      if (token === epoch) receive(data, startMono, startWall);
    } catch (_) { if (token === epoch) withdraw("disconnected", "transport_failure"); }
    finally { clearTimeout(timeout); if (inflight === controller) inflight = null; }
  }

  async function choose(scenario) {
    if (busy) return;
    busy = true;
    invalidateViewRequests();
    controls.forEach((button) => { button.disabled = true; });
    withdraw("unknown", "superseded", "正在切换模拟事件。");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 800);
    try {
      const response = await fetch("/api/demo", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario }), signal: controller.signal });
      if (!response.ok) throw new Error("unavailable");
    } catch (_) { withdraw("disconnected", "transport_failure"); }
    finally { clearTimeout(timeout); busy = false; controls.forEach((button) => { button.disabled = false; }); }
    await poll();
    await refreshReport();
  }

  const outcomes = {
    awaiting_result: "等待结果", awaiting_paint_report: "等待绘制回报", paint_opportunity_reported: "绘制机会已回报",
    result_timeout: "结果超时", display_timeout: "显示回报超时", no_result: "无结果", unknown: "未知",
  };
  let reportPending = false;
  async function refreshReport() {
    if (compact || document.hidden || reportPending) return;
    reportPending = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 800);
    try {
      const response = await fetch("/api/report", { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error("unavailable");
      const data = await response.json();
      if (data.schema_version !== "timed-overlay-report-v1" || !Array.isArray(data.samples)) throw new Error("invalid");
      put("opportunities", String(data.opportunities));
      put("successes", String(data.outcomes.paint_opportunity_reported || 0));
      put("timeouts", String((data.outcomes.result_timeout || 0) + (data.outcomes.display_timeout || 0)));
      put("no-results", String(data.outcomes.no_result || 0));
      const fmt = (x) => x === null ? "—" : `${x.toFixed(1)} ms`;
      put("latency", `收帧 → 回报收达：P50 ${fmt(data.frame_to_report_received_ms.p50)} / P95 ${fmt(data.frame_to_report_received_ms.p95)} · ${data.sample_count_with_report} 个回报`);
      const rows = data.samples.slice(-8).reverse().map((sample) => {
        const tr = document.createElement("tr");
        for (const text of [sample.sample_id, outcomes[sample.outcome] || "已撤回 / 未回报", fmt(sample.frame_to_report_received_ms)]) {
          const td = document.createElement("td"); td.textContent = text; tr.append(td);
        }
        return tr;
      });
      if (rows.length) byId("samples").replaceChildren(...rows);
      put("report-note", data.journal_available ? `全部 ${data.opportunities} 个样本均保留结果；此处显示最近 8 个。拒绝输入 ${data.counters.rejected_inputs || 0} 次。` : "日志不可用，提示已撤回。");
    } catch (_) { put("report-note", "报告暂不可用；已保存的日志不会因读取失败被删除。"); }
    finally { clearTimeout(timer); reportPending = false; }
  }

  controls.forEach((button) => button.addEventListener("click", () => choose(button.dataset.scenario)));
  document.addEventListener("visibilitychange", () => {
    invalidateViewRequests();
    withdraw("unknown", "hidden", "窗口可见性改变，等待新样本。");
    if (!document.hidden) poll();
  });
  window.addEventListener("pagehide", () => {
    pageActive = false;
    invalidateViewRequests();
    withdraw("unknown", "pagehide");
  });
  window.addEventListener("pageshow", (event) => {
    const resuming = !pageActive || event.persisted;
    pageActive = true;
    invalidateViewRequests();
    if (resuming) withdraw("unknown", "resume");
    poll();
  });
  setInterval(tick, 50);
  setInterval(poll, 150);
  setInterval(refreshReport, 600);
  poll(); refreshReport();
})();
