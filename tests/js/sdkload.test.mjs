// Run with: node --test tests/js/sdkload.test.mjs (also through pytest, see test_split_plan_js.py)
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const { load } = createRequire(import.meta.url)("../../tellybox/web/receiver/sdkload.js");

// A document whose <script> elements succeed or fail as scripted, and a wait that records its delays.
function harness(outcomes) {
  const scripts = [];
  const doc = {
    head: {
      appendChild(s) {
        scripts.push(s);
        s.parentNode = doc.head;
        const ok = outcomes[scripts.length - 1];
        queueMicrotask(() => (ok ? s.onload() : s.onerror()));
      },
      removeChild(s) { s.parentNode = null; },
    },
    createElement: () => ({ parentNode: null }),
  };
  const delays = [];
  const wait = (fn, ms) => { delays.push(ms); queueMicrotask(fn); };
  return { doc, scripts, delays, wait };
}

const run = (h, opts = {}) => new Promise((resolve) => {
  load(h.doc, "//sdk.test/cast.js", { wait: h.wait, ...opts }, (n) => resolve({ ok: true, n }), (n) => resolve({ ok: false, n }));
});

test("the first attempt succeeds: one script, no waiting", async () => {
  const h = harness([true]);
  assert.deepEqual(await run(h), { ok: true, n: 1 });
  assert.equal(h.scripts.length, 1);
  assert.equal(h.scripts[0].src, "//sdk.test/cast.js");
  assert.deepEqual(h.delays, []);
});

test("a failed script is retried after 2 s, then 4 s, each with a fresh element", async () => {
  const h = harness([false, false, true]);
  assert.deepEqual(await run(h), { ok: true, n: 3 });
  assert.deepEqual(h.delays, [2000, 4000]);
  assert.equal(new Set(h.scripts).size, 3);
  assert.equal(h.scripts[0].parentNode, null);  // a failed element is removed
  assert.equal(h.scripts[1].parentNode, null);
});

test("it gives up after three attempts", async () => {
  const h = harness([false, false, false, true]);
  assert.deepEqual(await run(h), { ok: false, n: 3 });
  assert.equal(h.scripts.length, 3);
  assert.deepEqual(h.delays, [2000, 4000]);
});

test("the delays can be changed", async () => {
  const h = harness([false, true]);
  assert.deepEqual(await run(h, { delays: [10] }), { ok: true, n: 2 });
  assert.deepEqual(h.delays, [10]);
});
