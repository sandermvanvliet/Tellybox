// Run with: node --test tests/js/split_mark.test.mjs (also through pytest, see test_split_plan_js.py)
import assert from "node:assert/strict";
import test from "node:test";

import * as plan from "../../tellybox/web/admin/static/split_plan.js";
import * as mark from "../../tellybox/web/admin/static/split_mark.js";

const near = (actual, expected, eps = 1e-3) => {
  assert.equal(actual.length, expected.length);
  actual.forEach((v, i) => assert.ok(Math.abs(v - expected[i]) <= eps, `${actual} vs ${expected}`));
};

test("contentBox: a 16:9 video in a 16:9 element fills it", () => {
  assert.deepEqual(mark.contentBox(640, 360, 1280, 720), { x: 0, y: 0, w: 640, h: 360 });
});

test("contentBox: pillarbox, the element is wider than the video", () => {
  // 4:3 video in a 800x400 element (the max-height clamps the video): bars left and right
  const box = mark.contentBox(800, 400, 640, 480);
  near([box.x, box.y, box.w, box.h], [133.333, 0, 533.333, 400]);
});

test("contentBox: letterbox, the element is taller than the video", () => {
  const box = mark.contentBox(400, 400, 1280, 720);
  near([box.x, box.y, box.w, box.h], [0, 87.5, 400, 225]);
});

test("contentBox: unknown sizes give null", () => {
  assert.equal(mark.contentBox(0, 300, 1280, 720), null);
  assert.equal(mark.contentBox(400, 300, 0, 0), null);
  assert.equal(mark.contentBox(400, 300, NaN, 720), null);
});

test("toNorm: a point in the content box, relative to the frame and clamped into it", () => {
  const box = { x: 0, y: 87.5, w: 400, h: 225 };
  assert.deepEqual(mark.toNorm(box, { x: 200, y: 200 }), { x: 0.5, y: 0.5 });
  assert.deepEqual(mark.toNorm(box, { x: -50, y: 10 }), { x: 0, y: 0 }); // on the bar above the frame
  assert.deepEqual(mark.toNorm(box, { x: 900, y: 999 }), { x: 1, y: 1 });
});

test("dragRegion: any direction gives the same region", () => {
  const box = { x: 0, y: 0, w: 400, h: 200 };
  const expected = [0.25, 0.25, 0.5, 0.5];
  near(mark.dragRegion(box, { x: 100, y: 50 }, { x: 300, y: 150 }), expected);
  near(mark.dragRegion(box, { x: 300, y: 150 }, { x: 100, y: 50 }), expected);
  near(mark.dragRegion(box, { x: 300, y: 50 }, { x: 100, y: 150 }), expected);
  near(mark.dragRegion(box, { x: 100, y: 150 }, { x: 300, y: 50 }), expected);
});

test("dragRegion: measured against the frame, not the element, when letterboxed", () => {
  const box = mark.contentBox(400, 400, 1280, 720); // frame at y 87.5..312.5
  near(mark.dragRegion(box, { x: 0, y: 87.5 }, { x: 200, y: 200 }), [0, 0, 0.5, 0.5]);
});

test("dragRegion: clamps a drag that leaves the frame", () => {
  const box = { x: 50, y: 0, w: 300, h: 300 };
  near(mark.dragRegion(box, { x: 200, y: 150 }, { x: 999, y: 999 }), [0.5, 0.5, 0.5, 0.5]);
  near(mark.dragRegion(box, { x: -20, y: -20 }, { x: 200, y: 150 }), [0, 0, 0.5, 0.5]);
  near(mark.dragRegion(box, { x: -20, y: -20 }, { x: 999, y: 999 }), [0, 0, 1, 1]);
});

test("dragRegion: a tap or a sliver is not a region", () => {
  const box = { x: 0, y: 0, w: 400, h: 200 };
  assert.equal(mark.dragRegion(box, { x: 100, y: 100 }, { x: 100, y: 100 }), null);
  assert.equal(mark.dragRegion(box, { x: 100, y: 100 }, { x: 300, y: 101 }), null);
  assert.equal(mark.dragRegion(box, { x: 100, y: 100 }, { x: 101, y: 150 }), null);
  assert.equal(mark.dragRegion(null, { x: 1, y: 1 }, { x: 200, y: 200 }), null);
});

test("dragRegion: a region always fits the frame, as the server demands", () => {
  const box = { x: 0, y: 0, w: 333, h: 187 };
  const [x, y, w, h] = mark.dragRegion(box, { x: 333, y: 187 }, { x: 0.1, y: 0.1 });
  assert.ok(x >= 0 && y >= 0 && w > 0 && h > 0 && x + w <= 1.0001 && y + h <= 1.0001);
});

test("regionRect: back to element pixels, null is the whole frame", () => {
  const box = { x: 0, y: 87.5, w: 400, h: 225 };
  assert.deepEqual(mark.regionRect(box, null), { x: 0, y: 87.5, w: 400, h: 225 });
  assert.deepEqual(mark.regionRect(box, [0.5, 0.5, 0.25, 0.2]), { x: 200, y: 200, w: 100, h: 45 });
});

test("detectedAt matches a cut within 0.05 s, and nudging ends the match", () => {
  const detected = [{ at_s: 100.0, confidence: 0.9 }, { at_s: 250.5, confidence: 0.5 }];
  assert.equal(plan.detectedAt(detected, 100.04).confidence, 0.9);
  assert.equal(plan.detectedAt(detected, 250.5).confidence, 0.5);
  assert.equal(plan.detectedAt(detected, 101), null);
  assert.equal(plan.detectedAt(null, 100), null);
});

test("confidenceLevel: sure from 0.75, check from 0.4", () => {
  assert.deepEqual([1, 0.75, 0.74, 0.4, 0.39, 0].map(plan.confidenceLevel), ["sure", "sure", "check", "check", "unsure", "unsure"]);
});
