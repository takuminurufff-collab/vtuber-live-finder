// DOM integration checks with no network calls. Run with jsdom installed.
// npm install --no-save jsdom@26 && node --test tests/test_ui.cjs
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { JSDOM } = require("jsdom");

const html = fs.readFileSync("site/index.html", "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script, "Application script is missing");

function record(i) {
  return {
    id: "UC" + String(i).padStart(22, "0"), name: "VTuber " + i,
    subscribers: i, lives: i % 10, videos: i % 50, shorts: 0,
    is_live: false, latest: null,
  };
}

async function setup(channels) {
  const dom = new JSDOM(html, {
    url: "https://example.com/vtuber-live-finder/",
    runScripts: "outside-only",
  });
  const win = dom.window;
  win.setInterval = () => 0;
  win.fetch = async () => ({
    ok: true,
    json: async () => ({ updated_at: "2026-11-07T15:11:04Z", channels }),
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
    assert.equal(dom.window.getComputedStyle(more).display, "none");
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

test("hidden admin menu cannot appear before interaction", async () => {
  const dom = await setup([record(1)]);
  try {
    const admin = dom.window.document.getElementById("admin");
    assert.equal(admin.hidden, true);
    assert.equal(dom.window.getComputedStyle(admin).display, "none");
  } finally {
    dom.window.close();
  }
});
