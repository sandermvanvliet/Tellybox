// Run with: node --test tests/js/player.test.mjs (also through pytest, see test_split_plan_js.py)
// Only the pure helpers of the kid app's in-app player; the DOM part needs a browser.
import assert from "node:assert/strict";
import test from "node:test";

import { getDeviceId, heartbeatBody, heartbeatState, makeDeviceId, outcomeFor, readTarget, targetAllowed, writeTarget } from "../../tellybox/web/static/player.js";

const rand = (arr) => {
  arr.forEach((_, i) => (arr[i] = (i * 37 + 11) & 255));
  return arr;
};
const memoryStorage = (init = {}) => {
  const data = { ...init };
  return { getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => void (data[k] = String(v)), data };
};
const blocked = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };

test("device id: 32 characters from the allowed set", () => {
  const id = makeDeviceId(rand);
  assert.match(id, /^[A-Za-z0-9_-]{32}$/);
  assert.match(makeDeviceId((a) => a.fill(255)), /^[A-Za-z0-9_-]{32}$/);
});

test("device id: kept in storage, malformed values replaced, memory when storage throws", () => {
  const s = memoryStorage();
  const id = getDeviceId(s, rand);
  assert.equal(s.data["tellybox.device"], id);
  assert.equal(getDeviceId(s, () => assert.fail("not generated again")), id);
  const bad = memoryStorage({ "tellybox.device": "short!" });
  assert.match(getDeviceId(bad, rand), /^[A-Za-z0-9_-]{32}$/);
  const first = getDeviceId(blocked, rand);
  assert.match(first, /^[A-Za-z0-9_-]{32}$/);
  assert.equal(getDeviceId(blocked, rand), first);
});

test("target toggle only when every picked profile can watch in the app", () => {
  const profiles = [
    { profile_id: 1, watch_in_app: true },
    { profile_id: 2, watch_in_app: false },
    { profile_id: 3 },
  ];
  assert.equal(targetAllowed(profiles, [1]), true);
  assert.equal(targetAllowed(profiles, [1, 2]), false);
  assert.equal(targetAllowed(profiles, [3]), false);
  assert.equal(targetAllowed(profiles, []), false);
  assert.equal(targetAllowed(profiles, [9]), false);
});

test("target: tv by default and whenever not allowed", () => {
  const s = memoryStorage();
  assert.equal(readTarget(s, true), "tv");
  writeTarget(s, "device");
  assert.equal(readTarget(s, true), "device");
  assert.equal(readTarget(s, false), "tv");
  assert.equal(readTarget(blocked, true), "tv");
  assert.doesNotThrow(() => writeTarget(blocked, "device"));
});

test("heartbeat state mapping", () => {
  const base = { error: false, ended: false, paused: false, waiting: false, readyState: 4 };
  assert.equal(heartbeatState(base), "playing");
  assert.equal(heartbeatState({ ...base, readyState: 3 }), "playing");
  assert.equal(heartbeatState({ ...base, readyState: 2 }), "buffering");
  assert.equal(heartbeatState({ ...base, waiting: true }), "buffering");
  assert.equal(heartbeatState({ ...base, paused: true, readyState: 1 }), "paused");
  assert.equal(heartbeatState({ ...base, paused: true, waiting: true }), "paused");
  assert.equal(heartbeatState({ ...base, ended: true, paused: true }), "ended");
  assert.equal(heartbeatState({ ...base, error: true, ended: true }), "error");
});

test("heartbeat body: position always, duration only when known", () => {
  const v = { error: false, ended: false, paused: false, waiting: false, readyState: 4, currentTime: 12.5, duration: 600 };
  assert.deepEqual(heartbeatBody("d", v), { device_id: "d", state: "playing", position_s: 12.5, duration_s: 600 });
  const unknown = heartbeatBody("d", { ...v, duration: NaN, currentTime: NaN });
  assert.deepEqual(unknown, { device_id: "d", state: "playing", position_s: 0 });
  assert.equal("duration_s" in heartbeatBody("d", { ...v, duration: Infinity }), false);
});

test("answers map to outcomes", () => {
  const next = { session: "s2", url: "/media/2/1/sig.mp4?s=s2", start_s: 3 };
  assert.deepEqual(outcomeFor({ action: "continue", reason: null, time_up: false }), { kind: "continue" });
  assert.deepEqual(outcomeFor({ action: "continue", reason: null, time_up: true, grace_deadline: "x" }), { kind: "continue" });
  assert.deepEqual(outcomeFor({ action: "next", reason: "finished", next }), { kind: "next", next });
  assert.deepEqual(outcomeFor({ action: "next", reason: "finished" }), { kind: "library" });
  for (const reason of ["time_up", "blocked", "stop_now"]) assert.deepEqual(outcomeFor({ action: "stop", reason }), { kind: "time_up" });
  for (const reason of ["replaced", "disconnected", "unknown_session", "finished", null]) assert.deepEqual(outcomeFor({ action: "stop", reason }), { kind: "library" });
  assert.deepEqual(outcomeFor({ action: "stop", reason: "error" }), { kind: "error" });
  assert.deepEqual(outcomeFor(null), { kind: "continue" });
});
