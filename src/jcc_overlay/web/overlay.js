"use strict";

(() => {
  const byId = (id) => document.getElementById(id);
  if (new URLSearchParams(location.search).get("compact") === "1") document.body.classList.add("compact");
  const hint = byId("hint");
  const target = byId("target");
  const action = byId("action");
  const reason = byId("reason");
  const label = byId("state-label");
  const countdown = byId("countdown");
  const lifetime = byId("lifetime");
  const controls = [...document.querySelectorAll("[data-scenario], #play")];
  const copy = {
    unknown: ["信息不足", "信息不足，暂不建议", "等待新的演示信息。"],
    conflict: ["信息冲突", "信息不一致，暂不建议", "两份演示信息不一致，旧提示已撤下。"],
    disconnected: ["已断连", "连接中断，提示已撤下", "收到新的演示提示后再显示。"],
    expired: ["已失效", "提示已到期", "旧提示已撤下，等待新的演示提示。"],
    invalidated: ["已失效", "画面变化，提示已撤下", "演示内容已变化，需要重新判断。"],
  };
  let session = null;
  let revision = -1;
  let retiredRevision = -1;
  let current = null;
  let requestEpoch = 0;
  let inflight = null;
  let busy = false;
  const put = (node, text) => { if (node.textContent !== text) node.textContent = text; };

  function clear(state, customDetail) {
    const [status, headline, detail] = copy[state] || copy.unknown;
    current = null;
    hint.dataset.state = state in copy ? state : "unknown";
    put(target, "");
    target.hidden = true;
    put(action, headline);
    put(reason, customDetail || detail);
    put(label, status);
    countdown.textContent = "";
    countdown.hidden = true;
    lifetime.hidden = true;
    lifetime.value = 0;
  }

  function withdraw(state, detail) {
    retiredRevision = Math.max(retiredRevision, revision);
    clear(state, detail);
  }

  function elapsed(startMono, startWall) {
    const mono = performance.now() - startMono;
    const wall = Date.now() - startWall;
    return mono < 0 || wall < 0 ? Infinity : Math.max(mono, wall);
  }

  function tick() {
    if (!current) return;
    const age = elapsed(current.startMono, current.startWall);
    const remaining = current.remaining - age;
    if (remaining <= 0) { withdraw("expired"); return; }
    if (age >= current.lease) { withdraw("disconnected"); return; }
    const state = remaining <= 3000 ? "expiring" : "valid";
    hint.dataset.state = state;
    label.textContent = state === "expiring" ? "即将到期" : "演示有效";
    countdown.textContent = `${(Math.floor(remaining / 100) / 10).toFixed(1)} 秒`;
    lifetime.value = remaining;
  }

  function validText(value, maximum) {
    return typeof value === "string" && value.trim() === value && value.length > 0 && value.length <= maximum && !/[\u0000-\u001f\u007f]/.test(value);
  }

  function receive(data, startMono, startWall) {
    if (!data || data.schema_version !== "overlay-demo-v1" || data.sample_kind !== "synthetic" || data.simulation !== true || data.execution_eligible !== false || !validText(data.session_id, 80) || !Number.isSafeInteger(data.revision) || data.revision < 0) {
      withdraw("unknown"); return;
    }
    if (session !== data.session_id) {
      withdraw("unknown");
      session = data.session_id;
      revision = -1;
      retiredRevision = -1;
    }
    if (data.revision < revision) return;
    const previous = current;
    const previousRevision = revision;
    revision = data.revision;
    put(byId("play-note"), data.playing ? "正在试播：有效 → 即将到期 → 失效 → 信息不足 → 冲突 → 断连。" : "选择任一状态，也可观看一次自动变化。");
    if (!["valid", "expiring"].includes(data.status)) {
      withdraw(data.status in copy ? data.status : "unknown");
      return;
    }
    const a = data.advice;
    if (!a || !validText(a.id, 80) || !validText(a.target, 28) || !validText(a.action, 20) || !validText(a.reason, 60) || !Number.isFinite(data.remaining_ms) || data.remaining_ms <= 0 || data.remaining_ms > 30000 || !Number.isFinite(data.lease_ms) || data.lease_ms <= 0 || data.lease_ms > 1200 || data.lease_ms > data.remaining_ms) {
      withdraw("unknown"); return;
    }
    if (document.hidden || data.revision <= retiredRevision) return;
    const age = elapsed(startMono, startWall);
    let remaining = data.remaining_ms - age;
    if (previous && previousRevision === revision) {
      if (previous.id !== a.id) { withdraw("unknown"); return; }
      remaining = Math.min(remaining, previous.remaining - elapsed(previous.startMono, previous.startWall));
    }
    if (remaining <= 0) { withdraw("expired"); return; }
    const lease = data.lease_ms - age;
    if (lease <= 0) { withdraw("disconnected"); return; }
    current = { id: a.id, remaining, lease, startMono: performance.now(), startWall: Date.now() };
    put(target, a.target);
    target.hidden = false;
    if (action.textContent !== a.action) action.textContent = a.action;
    if (reason.textContent !== a.reason) reason.textContent = a.reason;
    countdown.hidden = false;
    lifetime.max = Math.max(lifetime.max, remaining);
    lifetime.hidden = false;
    tick();
  }

  async function request(scenario) {
    if (inflight) return;
    const token = requestEpoch;
    const controller = new AbortController();
    inflight = controller;
    const startMono = performance.now();
    const startWall = Date.now();
    const timeout = setTimeout(() => controller.abort(), 900);
    try {
      const response = await fetch(scenario ? "/api/demo" : "/api/view", {
        method: scenario ? "POST" : "GET",
        headers: scenario ? { "Content-Type": "application/json" } : {},
        body: scenario ? JSON.stringify({ scenario }) : undefined,
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("unavailable");
      const data = await response.json();
      if (token === requestEpoch) receive(data, startMono, startWall);
    } catch (_) {
      if (token === requestEpoch) withdraw("disconnected");
    } finally {
      clearTimeout(timeout);
      if (inflight === controller) inflight = null;
    }
  }

  async function choose(scenario) {
    if (busy) return;
    busy = true;
    controls.forEach((button) => { button.disabled = true; });
    requestEpoch += 1;
    if (inflight) inflight.abort();
    inflight = null;
    withdraw("unknown", "正在切换演示状态。");
    await request(scenario);
    controls.forEach((button) => { button.disabled = false; });
    busy = false;
  }

  controls.forEach((button) => button.addEventListener("click", () => choose(button.id === "play" ? "play" : button.dataset.scenario)));
  byId("collapse").addEventListener("click", () => {
    tick();
    const collapsed = hint.classList.toggle("collapsed");
    byId("collapse").textContent = collapsed ? "展开" : "收起";
    byId("collapse").setAttribute("aria-expanded", String(!collapsed));
  });
  byId("open-window").addEventListener("click", () => {
    const popup = window.open("/?compact=1", "jcc-overlay-synthetic", "popup,width=460,height=270");
    if (!popup) byId("window-note").textContent = "浏览器拦住了小窗；可在地址末尾加 ?compact=1 查看。";
  });
  document.addEventListener("visibilitychange", () => {
    requestEpoch += 1;
    if (inflight) inflight.abort();
    inflight = null;
    withdraw("unknown", "窗口已切换，等待新的演示提示。");
    if (!document.hidden) request();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) withdraw("unknown", "窗口已恢复，等待新的演示提示。");
  });
  setInterval(tick, 75);
  setInterval(() => { if (!document.hidden && !busy) request(); }, 300);
  request();
})();
