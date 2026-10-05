// Run with: node --test tests/js/empty_home.test.mjs
import assert from "node:assert/strict";
import test from "node:test";

import { isEmptyHome } from "../../tellybox/web/static/api.js";

test("KA-15: empty home answer is an empty state", () => {
  assert.equal(isEmptyHome({ continue: [], shows: [] }), true);
  assert.equal(isEmptyHome({}), true);
});

test("KA-15: any show or continue tile is not empty", () => {
  assert.equal(isEmptyHome({ continue: [], shows: [{ show_id: 1 }] }), false);
  assert.equal(isEmptyHome({ continue: [{ episode_id: 1 }], shows: [] }), false);
});
