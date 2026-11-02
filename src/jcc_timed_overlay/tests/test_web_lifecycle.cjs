"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8");
const flush = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function ready(revision, sample = "synthetic-sample", session = "synthetic-session") {
  return {
    schema_version: "timed-overlay-view-v1", simulation: true, execution_eligible: false,
    session_id: session, revision, status: "ready", input_mode: "jsonl_tail",
    source_kind: "synthetic", sample_id: sample, render_token: "token-" + sample,
    message: "模拟计时 · 仅测试显示", remaining_ms: 4000, lease_ms: 1000,
  };
}

// Execute the actual page script. Only DOM, scheduling, and fetch boundaries
// are controlled; aborted requests deliberately can still finish to test epochs.
function page() {
  const nodes = new Map(), windowEvents = new Map(), documentEvents = new Map();
  const intervals = new Map(), frames = [], views = [], reports = [];
  let mono = 100, wall = 10_000, timer = 0;
  const element = (id) => {
    if (!nodes.has(id)) nodes.set(id, {
      textContent: id === "sample-id" ? "—" : "", hidden: false, value: 0, dataset: {},
      getBoundingClientRect: () => ({ width: 300, height: 200, top: 0, left: 0, bottom: 200, right: 300 }),
    });
    return nodes.get(id);
  };
  const document = {
    hidden: false, getElementById: element, querySelectorAll: () => [],
    body: { classList: { add() {} } },
    addEventListener: (event, callback) => documentEvents.set(event, callback),
  };
  const context = {
    document, window: { addEventListener: (event, callback) => windowEvents.set(event, callback) },
    location: { search: "?compact=1" }, URLSearchParams, AbortController,
    crypto: { randomUUID: () => "synthetic-client" }, performance: { now: () => mono },
    Date: class extends Date { static now() { return wall; } }, innerHeight: 600, innerWidth: 800,
    setInterval: (callback, ms) => intervals.set(ms, callback),
    setTimeout: () => ++timer, clearTimeout() {},
    requestAnimationFrame: (callback) => frames.push(callback),
    fetch: (url, options) => {
      if (url === "/api/view") {
        const headers = deferred(), body = deferred();
        const request = { headers, body, signal: options.signal };
        views.push(request);
        return headers.promise;
      }
      assert.equal(url, "/api/browser-event");
      reports.push(JSON.parse(options.body));
      return Promise.resolve({ ok: true });
    },
  };
  vm.runInNewContext(source, context, { filename: "app.js" });
  return {
    document, views, reports, element,
    windowEvent: (event, data = {}) => windowEvents.get(event)(data),
    visibility: () => documentEvents.get("visibilitychange")(),
    poll: () => { intervals.get(150)(); },
    advance: (ms) => { mono += ms; wall += ms; },
    async headers(request) {
      request.headers.resolve({ ok: true, json: () => request.body.promise });
      await flush();
    },
    async respond(request, value) {
      request.headers.resolve({ ok: true, json: () => request.body.promise });
      request.body.resolve(value);
      await flush();
    },
    async paint() {
      while (frames.length) frames.shift()();
      await flush();
    },
  };
}

test("pagehide rejects an old ready response and pauses polling until pageshow", async () => {
  const p = page(), old = p.views[0];
  p.windowEvent("pagehide");
  await p.respond(old, ready(1, "old-before-pagehide"));
  assert.equal(p.element("sample-id").textContent, "—");
  assert.equal(old.signal.aborted, true);
  await p.poll(); // document.hidden can still be false at the lifecycle boundary.
  assert.equal(p.views.length, 1);
  p.windowEvent("pageshow", { persisted: true });
  assert.equal(p.views.length, 2);
  await p.respond(p.views[1], ready(2, "fresh-after-pageshow"));
  assert.equal(p.element("sample-id").textContent, "fresh-after-pageshow");
});

test("the initial pageshow does not retire an already displayed first revision", async () => {
  const p = page();
  await p.respond(p.views[0], ready(1, "first-load"));
  p.windowEvent("pageshow", { persisted: false });
  assert.equal(p.element("sample-id").textContent, "first-load");
  await p.respond(p.views[1], ready(1, "first-load"));
  assert.equal(p.element("sample-id").textContent, "first-load");
});

for (const persisted of [false, true]) {
  test(`pageshow persisted=${persisted} fences a response whose JSON body is still in flight`, async () => {
    const p = page(), old = p.views[0];
    await p.headers(old);
    p.windowEvent("pageshow", { persisted });
    await p.respond(old, ready(1, "old-body"));
    assert.equal(p.element("sample-id").textContent, "—");
    assert.equal(old.signal.aborted, true);
    assert.equal(p.views.length, 2);
    await p.respond(p.views[1], ready(2, "fresh-body"));
    assert.equal(p.element("sample-id").textContent, "fresh-body");
  });
}

test("pagehide/pageshow keep retired revisions sealed and allow a fresh revision", async () => {
  const p = page();
  await p.respond(p.views[0], ready(1, "shown-before-hide"));
  await p.poll();
  const old = p.views[1];
  await p.headers(old);
  p.windowEvent("pagehide");
  p.windowEvent("pageshow", { persisted: true });
  const fresh = p.views[2];
  await p.respond(old, ready(2, "unseen-old-response"));
  assert.equal(p.element("sample-id").textContent, "—");
  await p.respond(fresh, ready(1, "shown-before-hide"));
  assert.equal(p.element("sample-id").textContent, "—");
  await p.poll();
  await p.respond(p.views[3], ready(3, "new-revision"));
  assert.equal(p.element("sample-id").textContent, "new-revision");
});

test("a late pre-pagehide failure cannot withdraw a newer display", async () => {
  const p = page(), old = p.views[0];
  p.windowEvent("pagehide");
  p.windowEvent("pageshow", { persisted: true });
  assert.equal(p.views.length, 2);
  await p.respond(p.views[1], ready(2, "new-display"));
  old.headers.reject(new Error("synthetic old transport failure"));
  await flush();
  assert.equal(p.element("sample-id").textContent, "new-display");
});

test("a late pre-pagehide session cannot replace the new session", async () => {
  const p = page(), old = p.views[0];
  p.windowEvent("pagehide");
  p.windowEvent("pageshow", { persisted: true });
  assert.equal(p.views.length, 2);
  await p.respond(p.views[1], ready(1, "new-session-frame", "new-session"));
  await p.respond(old, ready(99, "old-session-frame", "old-session"));
  assert.equal(p.element("sample-id").textContent, "new-session-frame");
});

test("visibilitychange still fences old requests and resumes with a new one", async () => {
  const p = page(), old = p.views[0];
  p.document.hidden = true;
  p.visibility();
  await p.respond(old, ready(1, "old-hidden-frame"));
  assert.equal(p.element("sample-id").textContent, "—");
  assert.equal(old.signal.aborted, true);
  p.document.hidden = false;
  p.visibility();
  await p.respond(p.views[1], ready(2, "new-visible-frame"));
  assert.equal(p.element("sample-id").textContent, "new-visible-frame");
});

test("queued drawing callbacks after pagehide never report a visible paint", async () => {
  const p = page();
  await p.respond(p.views[0], ready(1));
  p.windowEvent("pagehide");
  await p.paint();
  assert.equal(p.element("sample-id").textContent, "—");
  assert.equal(p.reports.some((report) => report.stage === "dom_updated"), true);
  assert.equal(p.reports.some((report) => report.stage === "paint_opportunity"), false);
});
