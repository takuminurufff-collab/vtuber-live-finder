// Node.js built-in test runner. No browser, API key, or external packages required.
// Run: node --test tests/test_filters.cjs
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");

const html = fs.readFileSync("site/index.html", "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1];
assert.ok(script, "Missing application script");
new vm.Script(script, { filename: "site/index.html" });

const fieldKeys = ["subs", "lives", "videos", "shorts", "viewers"];
for (const key of fieldKeys) {
  assert.ok(html.includes('id="f-' + key + '-min"'), key + " min input missing");
  assert.ok(html.includes('id="f-' + key + '-max"'), key + " max input missing");
}

const begin = script.indexOf("  function passes(c) {");
const finish = script.indexOf("  function card(c) {", begin);
assert.ok(begin >= 0 && finish > begin, "Missing passes() implementation");
const passFunction = script.slice(begin, finish);

function check(filters, channel) {
  const scope = {
    f: {
      st: "all", subsMin: "", subsMax: "", livesMin: "", livesMax: "",
      videosMin: "", videosMax: "", shortsMin: "", shortsMax: "",
      viewersMin: "", viewersMax: "", ...filters,
    },
    FIELDS: fieldKeys,
    METRICS: { subs: "subscribers", lives: "lives", videos: "videos", shorts: "shorts" },
    c: channel,
  };
  vm.createContext(scope);
  return vm.runInContext(passFunction + "\npasses(c)", scope);
}

const ordinary = { is_live: false, subscribers: 15, lives: 3, videos: 8, shorts: 0 };
const live = { ...ordinary, is_live: true, live: { viewers: 0 } };

test("each count accepts inclusive lower and upper bounds", () => {
  for (const [field, n] of [["subs", 15], ["lives", 3], ["videos", 8], ["shorts", 0]]) {
    assert.equal(check({ [field+"Min"]: String(n), [field+"Max"]: String(n) }, ordinary), true, field);
    assert.equal(check({ [field+"Min"]: String(n+1) }, ordinary), false, field+" minimum");
    if (n > 0) assert.equal(check({ [field+"Max"]: String(n-1) }, ordinary), false, field+" maximum");
  }
});

test("zero concurrent viewers is valid, but unknown is not zero", () => {
  assert.equal(check({ viewersMin: "0", viewersMax: "0" }, live), true);
  assert.equal(check({ viewersMax: "0" }, { ...live, live: { viewers: null } }), false);
  assert.equal(check({ viewersMax: "0" }, ordinary), false);
});

test("all five numerical conditions combine with AND", () => {
  const all = { subsMin:"10", subsMax:"20", livesMax:"3", videosMin:"8", shortsMax:"0", viewersMax:"0" };
  assert.equal(check(all, live), true);
  assert.equal(check({ ...all, videosMin:"9" }, live), false);
  assert.equal(check({ ...all, viewersMin:"1" }, live), false);
});

test("unknown metrics are excluded only when that metric has a filter", () => {
  assert.equal(check({}, { ...ordinary, shorts:null }), true);
  assert.equal(check({ shortsMax:"0" }, { ...ordinary, shorts:null }), false);
});

test("live/off/all state filters work independently", () => {
  assert.equal(check({ st:"live" }, ordinary), false);
  assert.equal(check({ st:"off" }, live), false);
  assert.equal(check({ st:"all" }, ordinary), true);
});
