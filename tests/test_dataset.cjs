// Checks the saved dataset structure without calling YouTube or consuming quota.
// Run: node --test tests/test_dataset.cjs
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");

const data = JSON.parse(fs.readFileSync("site/data.json", "utf8"));
const channels = data.channels;

test("dataset is an array of uniquely identified YouTube channels", () => {
  assert.ok(Array.isArray(channels));
  const seen = new Set();
  for (const channel of channels) {
    assert.match(channel.id, /^UC[A-Za-z0-9_-]{22}$/);
    assert.ok(!seen.has(channel.id), "Duplicate channel: " + channel.id);
    seen.add(channel.id);
    assert.equal(typeof channel.name, "string");
    assert.equal(typeof channel.is_live, "boolean");
  }
});

test("numeric values are nonnegative, with unknown values represented by null", () => {
  for (const channel of channels) {
    for (const key of ["subscribers", "videos", "shorts", "lives"]) {
      const value = channel[key];
      if (value == null) continue;
      assert.ok(Number.isSafeInteger(value) && value >= 0, channel.id + ": invalid " + key);
    }
  }
});

test("live records contain valid viewer counts, but unknown concurrent viewership is allowed", () => {
  for (const channel of channels) {
    if (!channel.is_live) continue;
    assert.ok(channel.live && typeof channel.live.video_id === "string",
      channel.id + ": live channel without stream details");
    const viewers = channel.live.viewers;
    assert.ok(viewers == null || (Number.isSafeInteger(viewers) && viewers >= 0),
      channel.id + ": invalid concurrent viewer count");
  }
});

test("dataset records update timestamp for freshness messaging", () => {
  assert.ok(typeof data.updated_at === "string" && Number.isFinite(Date.parse(data.updated_at)));
});

test("live stream IDs are valid and not shared by multiple channels", () => {
  const seen = new Set();
  for (const channel of channels) {
    if (!channel.is_live) continue;
    const videoId = channel.live.video_id;
    assert.match(videoId, /^[A-Za-z0-9_-]{11}$/, channel.id + ": invalid live video ID");
    assert.ok(!seen.has(videoId), "Duplicate live stream: " + videoId);
    seen.add(videoId);
  }
});
