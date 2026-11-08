// DOM integration checks with no network calls. Run with jsdom installed.
// npm install --no-save jsdom@26 && node --test tests/test_ui.cjs
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync("site/index.html", "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script, "Application script is missing");
// jsdom does not fully model CSS !important cascade for computed display.
// Assert the author stylesheet explicitly honors the HTML hidden attribute.
assert.match(html, /\[hidden\]\s*\{\s*display:\s*none\s*!important;\s*\}/);

function record(i) {
  return {
    id: "UC" + String(i).padStart(22, "0"), name: "VTuber " + i,
    subscribers: i, lives: i % 10, videos: i % 50, shorts: 0,
    is_live: false, latest: null,
  };
}

async function setup(channels, updatedAt = "2026-11-07T15:11:04Z", nowAt = null) {
  const dom = new JSDOM(html, {
    url: "https://example.com/vtuber-live-finder/",
    runScripts: "outside-only",
  });
  const win = dom.window;
  win.setInterval = () => 0;
  if (nowAt) win.Date.now = () => Date.parse(nowAt);
  win.fetch = async () => ({
    ok: true,
    json: async () => ({ updated_at: updatedAt, channels }),
  });
  win.eval(script);
  // Allow the initial data.json fetch and render promises to complete.
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  return dom;
}

test("all saved VTubers remain reachable beyond the first 300", async () => {
  const dom = await setup(Array.from({ length: 651 }, (_, i) => record(i)));
  try {
    const doc = dom.window.document;
    doc.getElementById("st-all").click();
    const more = doc.getElementById("showMore");
    assert.equal(doc.querySelectorAll("a.card").length, 300);
    assert.equal(more.hidden, false);
    more.click();
    assert.equal(doc.querySelectorAll("a.card").length, 600);
    more.click();
    assert.equal(doc.querySelectorAll("a.card").length, 651);
    assert.equal(more.hidden, true);
    assert.equal(more.hasAttribute("hidden"), true);
    doc.getElementById("f-subs-min").value = "640";
    doc.getElementById("f-subs-min").dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    assert.equal(doc.querySelectorAll("a.card").length, 11);
    assert.equal(more.hidden, true);
  } finally {
    dom.window.close();
  }
});

test("data quality notice distinguishes unknown counts from zero", async () => {
  const dom = await setup([record(1), { ...record(2), videos: null, shorts: null, lives: null }]);
  try {
    const doc = dom.window.document;
    doc.getElementById("st-all").click();
    assert.match(doc.getElementById("dataQuality").textContent, /1件/);
    doc.getElementById("f-videos-max").value = "0";
    doc.getElementById("f-videos-max").dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    assert.equal(doc.querySelectorAll("a.card").length, 0);
  } finally {
    dom.window.close();
  }
});

test("unknown subscriber totals appear last and remain visible without a subscriber filter", async () => {
  const dom = await setup([
    { ...record(1), subscribers: null },
    { ...record(2), subscribers: 0 },
    { ...record(3), subscribers: 100 },
  ]);
  try {
    const doc = dom.window.document;
    doc.getElementById("st-all").click();
    doc.getElementById("sort").value = "subs";
    doc.getElementById("sort").dispatchEvent(new dom.window.Event("change", { bubbles: true }));
    const names = [...doc.querySelectorAll(".card .name")].map(e => e.textContent);
    assert.deepEqual(names, ["VTuber 2", "VTuber 3", "VTuber 1"]);
    assert.match(doc.getElementById("dataQuality").textContent, /登録者数が非公開または未取得：1件/);
    doc.getElementById("f-subs-min").value = "0";
    doc.getElementById("f-subs-min").dispatchEvent(new dom.window.Event("input", { bubbles: true }));
    assert.equal(doc.querySelectorAll("a.card").length, 2);
  } finally {
    dom.window.close();
  }
});

test("hidden admin menu cannot appear before interaction", async () => {
  const dom = await setup([record(1)]);
  try {
    const admin = dom.window.document.getElementById("admin");
    assert.equal(admin.hidden, true);
    assert.equal(admin.hasAttribute("hidden"), true);
  } finally {
    dom.window.close();
  }
});

test("unknown livestream status is labelled as unverified, not offline", async () => {
  const unknown = { ...record(1), is_live: null, last_live_at: "2026-11-07T14:00:00Z",
    live: { video_id: "abc12345678", title: "Unverified", viewers: 123 } };
  const dom = await setup([unknown]);
  try {
    const doc = dom.window.document;
    doc.getElementById("st-all").click();
    assert.equal(doc.querySelectorAll("a.card").length, 1);
    assert.match(doc.querySelector("a.card .badge").textContent, /配信状況を確認中/);
    assert.match(doc.getElementById("count").textContent, /配信状況未確認 1/);
    doc.getElementById("st-off").click();
    assert.equal(doc.querySelectorAll("a.card").length, 0);
  } finally {
    dom.window.close();
  }
});

test("fresh data does not show a stale livestream warning", async () => {
  const dom = await setup([record(1)], "2026-11-08T14:00:00Z", "2026-11-08T15:30:00Z");
  try {
    const notice = dom.window.document.getElementById("freshnessWarning");
    assert.equal(notice.hidden, true);
    assert.equal(notice.textContent, "");
  } finally {
    dom.window.close();
  }
});

test("data older than two hours warns that livestream status may be outdated", async () => {
  const dom = await setup([record(1)], "2026-11-08T12:59:00Z", "2026-11-08T15:00:00Z");
  try {
    const notice = dom.window.document.getElementById("freshnessWarning");
    assert.equal(notice.hidden, false);
    assert.match(notice.textContent, /配信中・同時接続者数/);
  } finally {
    dom.window.close();
  }
});

test("invalid update timestamp is shown as uncertain rather than current", async () => {
  const dom = await setup([record(1)], "not-a-date", "2026-11-08T15:00:00Z");
  try {
    const notice = dom.window.document.getElementById("freshnessWarning");
    assert.equal(notice.hidden, false);
    assert.match(notice.textContent, /データの更新が遅れています/);
    assert.equal(dom.window.document.getElementById("meta").textContent, "最終更新日時を確認できません");
    assert.doesNotMatch(dom.window.document.getElementById("meta").textContent, /NaN/);
  } finally {
    dom.window.close();
  }
});

async function simulateAdminRefresh(dom, failJson, previousPolls = 0, apiErrorPolls = 0) {
  const win = dom.window;
  const doc = win.document;
  let runLookups = 0;
  let datasetLookups = 0;
  let dispatchedRequestId = null;
  const fetchUrls = [];
  win.setTimeout = (callback) => { callback(); return 0; };
  const now = win.Date.now();
  win.Date.now = () => now;
  win.fetch = async (url, options) => {
    fetchUrls.push(url);
    if (url.includes("/dispatches")) {
      assert.equal(options.method, "POST");
      const payload = JSON.parse(options.body);
      dispatchedRequestId = payload.inputs.request_id;
      assert.ok(dispatchedRequestId.length > 10);
      return { status: 204 };
    }
    if (url.includes("/actions/workflows/update.yml/runs?")) {
      runLookups++;
      if (runLookups <= apiErrorPolls) return { ok: false, status: 502 };
      const isPrevious = runLookups <= apiErrorPolls + previousPolls;
      const unrelated = {
        id: 300, display_title: "配信データ更新 [another-person]",
        created_at: new Date(now).toISOString(), status: "completed", conclusion: "failure",
      };
      const mine = {
        id: 200, display_title: "配信データ更新 [" + dispatchedRequestId + "]",
        created_at: new Date(now).toISOString(), status: "completed", conclusion: "success",
      };
      return {
        ok: true,
        json: async () => ({ workflow_runs: isPrevious ? [unrelated] : [unrelated, mine] }),
      };
    }
    if (url.includes("data.json")) {
      datasetLookups++;
      if (failJson === true) throw new Error("network failed");
      return { ok: true, json: async () => ({
        updated_at: failJson === "stale" ? "2026-11-07T15:11:04Z" : new Date(now).toISOString(),
        channels: [record(1)],
      }) };
    }
    throw new Error("Unexpected fetch: " + url);
  };
  doc.getElementById("token").value = "test-token";
  doc.getElementById("saveToken").click();
  doc.getElementById("refreshBtn").click();
  for (let i = 0; i < 12; i++) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  doc.__refreshCounters = { runLookups, datasetLookups, fetchUrls, dispatchedRequestId };
  return doc;
}

test("admin refresh reports JSON loading failure rather than false success", async () => {
  const dom = await setup([record(1)]);
  try {
    const doc = await simulateAdminRefresh(dom, true);
    const status = doc.getElementById("refreshStatus");
    assert.match(status.textContent, /最新データの反映を確認できませんでした/);
    assert.doesNotMatch(status.textContent, /^更新しました。$/);
    assert.match(status.className, /err/);
    assert.equal(doc.getElementById("refreshBtn").disabled, false);
  } finally {
    dom.window.close();
  }
});

test("admin refresh ignores a recently finished run from before dispatch", async () => {
  const dom = await setup([record(1)]);
  try {
    const doc = await simulateAdminRefresh(dom, false, 3);
    assert.equal(doc.__refreshCounters.runLookups, 4);
    assert.equal(doc.__refreshCounters.datasetLookups, 1);
    assert.equal(doc.getElementById("refreshBtn").disabled, false);
    assert.doesNotMatch(doc.getElementById("refreshStatus").className, /err/);
  } finally {
    dom.window.close();
  }
});

test("admin polls only the update workflow on main instead of unrelated actions", async () => {
  const dom = await setup([record(1)]);
  try {
    const doc = await simulateAdminRefresh(dom, false);
    const runUrls = doc.__refreshCounters.fetchUrls.filter((url) => url.includes("/runs?"));
    assert.ok(runUrls.length >= 1);
    assert.ok(runUrls.every((url) =>
      url.includes("/actions/workflows/update.yml/runs?event=workflow_dispatch&branch=main")));
    assert.equal(doc.getElementById("refreshBtn").disabled, false);
  } finally {
    dom.window.close();
  }
});

test("admin retries temporary GitHub runs API failures and still loads new data", async () => {
  const dom = await setup([record(1)]);
  try {
    const doc = await simulateAdminRefresh(dom, false, 0, 2);
    assert.equal(doc.__refreshCounters.runLookups, 3);
    assert.equal(doc.__refreshCounters.datasetLookups, 1);
    assert.doesNotMatch(doc.getElementById("refreshStatus").className, /err/);
    assert.equal(doc.getElementById("refreshBtn").disabled, false);
  } finally {
    dom.window.close();
  }
});

test("admin correlates its own request even when a newer unrelated run has failed", async () => {
  const dom = await setup([record(1)]);
  try {
    const doc = await simulateAdminRefresh(dom, false);
    assert.ok(doc.__refreshCounters.dispatchedRequestId);
    assert.equal(doc.__refreshCounters.datasetLookups, 1);
    assert.doesNotMatch(doc.getElementById("refreshStatus").className, /err/);
    assert.equal(doc.getElementById("refreshBtn").disabled, false);
  } finally {
    dom.window.close();
  }
});

test("admin refresh never claims success while CDN serves unchanged JSON", async () => {
  const dom = await setup([record(1)]);
  try {
    const doc = await simulateAdminRefresh(dom, "stale");
    const status = doc.getElementById("refreshStatus");
    assert.match(status.textContent, /最新データの反映を確認できませんでした/);
    assert.doesNotMatch(status.textContent, /更新しました/);
    assert.match(status.className, /err/);
    assert.equal(doc.getElementById("refreshBtn").disabled, false);
  } finally {
    dom.window.close();
  }
});

test("admin refresh reports success when the new dataset is actually loaded", async () => {
  const dom = await setup([record(1)]);
  try {
    const doc = await simulateAdminRefresh(dom, false);
    const status = doc.getElementById("refreshStatus");
    // Success toast is cleared by an immediate fake timeout.
    assert.doesNotMatch(status.className, /err/);
    assert.equal(doc.getElementById("refreshBtn").disabled, false);
    assert.match(doc.getElementById("meta").textContent, /最終更新/);
    assert.equal(doc.getElementById("freshnessWarning").hidden, true);
  } finally {
    dom.window.close();
  }
});
