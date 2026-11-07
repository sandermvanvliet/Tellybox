// Run with: node --test tests/js/split_plan.test.mjs (also through pytest, see test_split_plan_js.py)
import assert from "node:assert/strict";
import test from "node:test";

import * as plan from "../../tellybox/web/admin/static/split_plan.js";

const three = () => plan.addCut(plan.addCut(plan.whole(600, "Show"), 100), 300);
const starts = (p) => p.map((s) => s.start_s);

test("addCut splits the segment at t and the right part is untitled", () => {
  const p = plan.addCut(plan.whole(600, "Show"), 100.0004);
  assert.deepEqual(p, [
    { start_s: 0, end_s: 100, title: "Show", keep: true },
    { start_s: 100, end_s: 600, title: "", keep: true },
  ]);
});

test("addCut: the right part inherits keep", () => {
  const p = plan.addCut(plan.setKeep(plan.whole(600), 0, false), 100);
  assert.deepEqual(p.map((s) => s.keep), [false, false]);
});

test("addCut refuses within 0.5 s of an edge or another cut", () => {
  const p = three();
  for (const t of [0, 0.4, 99.7, 100.3, 299.6, 599.8, 600]) assert.equal(plan.addCut(p, t), p, String(t));
  assert.equal(plan.addCut(p, 100.5).length, 4);
});

test("addCut finds the right segment in the middle", () => {
  const p = plan.addCut(three(), 200);
  assert.deepEqual(starts(p), [0, 100, 200, 300]);
  assert.deepEqual(p.map((s) => s.end_s), [100, 200, 300, 600]);
});

test("removeCut merges into the left segment and keeps its title and keep", () => {
  let p = plan.setTitle(three(), 0, "A");
  p = plan.setTitle(p, 1, "B");
  p = plan.setKeep(p, 0, false);
  const merged = plan.removeCut(p, 1);
  assert.deepEqual(merged[0], { start_s: 0, end_s: 300, title: "A", keep: false });
  assert.equal(merged.length, 2);
  assert.equal(plan.removeCut(p, 0), p);
  assert.equal(plan.removeCut(p, 9), p);
});

test("moveCut clamps between its neighbours plus 0.5 s", () => {
  const p = three();
  assert.deepEqual(starts(plan.moveCut(p, 1, 150)), [0, 150, 300]);
  assert.deepEqual(starts(plan.moveCut(p, 1, -50)), [0, 0.5, 300]);
  assert.deepEqual(starts(plan.moveCut(p, 1, 5000)), [0, 299.5, 300]);
  assert.deepEqual(starts(plan.moveCut(p, 2, 50)), [0, 100, 100.5]);
  assert.deepEqual(plan.moveCut(p, 1, 150)[0].end_s, 150);
  assert.equal(plan.moveCut(p, 0, 5), p);
});

test("moveCut leaves a plan with no room alone", () => {
  const p = [
    { start_s: 0, end_s: 0.6, title: "", keep: true },
    { start_s: 0.6, end_s: 0.9, title: "", keep: true },
  ];
  assert.equal(plan.moveCut(p, 1, 0.7), p);
});

test("nudge moves by a delta, rounded to milliseconds, and respects the clamp", () => {
  const p = three();
  assert.equal(plan.nudge(p, 1, 0.1)[1].start_s, 100.1);
  assert.equal(plan.nudge(plan.nudge(p, 1, 0.1), 1, 0.2)[1].start_s, 100.3);
  assert.equal(plan.nudge(p, 1, 1000)[1].start_s, 299.5);
  assert.equal(plan.nudge(p, 0, 1), p);
});

test("setTitle and setKeep change one segment and don't mutate", () => {
  const p = three();
  const q = plan.setKeep(plan.setTitle(p, 1, "Two"), 1, false);
  assert.equal(q[1].title, "Two");
  assert.equal(q[1].keep, false);
  assert.equal(p[1].title, "");
  assert.equal(p[1].keep, true);
});

test("fromChapters mirrors segments_from_chapters", () => {
  const chapters = [
    { start_s: 0, end_s: 300, title: "One" },
    { start_s: 300, end_s: 450, title: " Two " },
    { start_s: 450, end_s: 600, title: "" },
  ];
  assert.deepEqual(
    plan.fromChapters(chapters, 600).map((s) => [s.start_s, s.end_s, s.title, s.keep]),
    [[0, 300, "One", true], [300, 450, "Two", true], [450, 600, "", true]],
  );
});

test("fromChapters: the first segment may start before any chapter; chapters outside the video are ignored", () => {
  const p = plan.fromChapters(
    [{ start_s: 100, end_s: 200, title: "A" }, { start_s: 900, end_s: 950, title: "late" }, { start_s: 400, end_s: 450, title: "B" }],
    600,
  );
  assert.deepEqual(p.map((s) => [s.start_s, s.end_s, s.title]), [[0, 100, ""], [100, 400, "A"], [400, 600, "B"]]);
  assert.deepEqual(plan.fromChapters([], 600), plan.whole(600));
});

test("keptDuration counts only kept parts", () => {
  const p = plan.setKeep(three(), 0, false);
  assert.equal(plan.keptDuration(p), 500);
  assert.equal(plan.keptDuration(plan.whole(12.3456)), 12.346);
});

test("problems: a fine plan has none", () => {
  assert.deepEqual(plan.problems(three(), 600), []);
});

test("problems: leaving one part out of two is allowed", () => {
  const p = plan.setKeep(plan.addCut(plan.whole(600), 100), 0, false);
  assert.deepEqual(plan.problems(p, 600), []);
});

test("problems: single part, nothing kept, too short", () => {
  assert.deepEqual(plan.problems(plan.whole(600), 600), ["single"]);
  assert.deepEqual(plan.problems(three().map((s) => ({ ...s, keep: false })), 600), ["none"]);
  assert.deepEqual(plan.problems(plan.addCut(plan.whole(600), 2), 600), ["short"]);
  const dropped = plan.setKeep(plan.addCut(plan.whole(600), 0.5), 0, false);
  assert.deepEqual(plan.problems(dropped, 600), []);
  assert.deepEqual(plan.problems([{ start_s: 0, end_s: 3, title: "", keep: true }, { start_s: 3, end_s: 8, title: "", keep: false }], 8), ["short"]);
});

test("problems: shape errors come first", () => {
  assert.deepEqual(plan.problems([], 600), ["empty"]);
  const p = three();
  assert.deepEqual(plan.problems(p, 700), ["end"]);
  assert.deepEqual(plan.problems(p, 600.9), []);
  assert.deepEqual(plan.problems(p.map((s, i) => (i === 0 ? { ...s, start_s: 1 } : s)), 600), ["start"]);
  assert.deepEqual(plan.problems(p.map((s, i) => (i === 1 ? { ...s, start_s: 150 } : s)), 600), ["gap"]);
  assert.deepEqual(plan.problems(plan.setTitle(p, 0, "x".repeat(201)), 600), ["title"]);
  assert.deepEqual(plan.problems(plan.setTitle(p, 0, " ".repeat(300)), 600), []);
  assert.deepEqual(plan.problems([{ start_s: 0, end_s: 0, title: "", keep: true }], 0), ["gap"]);
  const many = Array.from({ length: 201 }, (_, i) => ({ start_s: i * 10, end_s: i * 10 + 10, title: "", keep: true }));
  assert.deepEqual(plan.problems(many, 2010), ["too_many"]);
});

test("indexAt", () => {
  const p = three();
  assert.deepEqual([0, 99.9, 100, 450, 600, 9999].map((t) => plan.indexAt(p, t)), [0, 0, 1, 2, 2, 2]);
});

test("stripFrameTime shows the title card for a detected cut, else the start frame", () => {
  const detected = [{ at_s: 41.6, title_hit_s: 42.0 }];
  assert.equal(plan.stripFrameTime(detected, { start_s: 41.6 }, 1), 42.5); // snapped to black: show the card
  assert.equal(plan.stripFrameTime(detected, { start_s: 60 }, 2), 60); // a manual cut: its own frame
  assert.equal(plan.stripFrameTime(detected, { start_s: 0 }, 0), 0); // the first part
  assert.equal(plan.stripFrameTime(null, { start_s: 41.6 }, 1), 41.6);
});

test("fromDuration: equal parts, a short rest is left out", () => {
  const p = plan.fromDuration(600, 3700);
  assert.deepEqual(starts(p), [0, 600, 1200, 1800, 2400, 3000, 3600]);
  assert.equal(p[6].end_s, 3700);
  assert.deepEqual(p.map((s) => s.keep), [true, true, true, true, true, true, false]);
});

test("fromDuration: a rest of half an episode or more is kept, a tiny one joins the last part", () => {
  assert.equal(plan.fromDuration(600, 2100).at(-1).keep, true);
  const p = plan.fromDuration(600, 1800.3);
  assert.deepEqual(starts(p), [0, 600, 1200]);
  assert.equal(p[2].end_s, 1800.3);
});

test("fromDuration refuses too short a length and too many parts", () => {
  assert.equal(plan.fromDuration(4, 600), null);
  assert.equal(plan.fromDuration(5, 5 * 201), null);
  assert.equal(plan.fromDuration(NaN, 600), null);
  assert.equal(plan.fromDuration(600, 600).length, 1);
});

test("rippleCut moves the cut and all later cuts by the delta", () => {
  const p = plan.rippleCut(plan.fromDuration(600, 1800), 1, 2);
  assert.deepEqual(starts(p), [0, 602, 1202]);
  assert.deepEqual(p.map((s) => s.end_s), [602, 1202, 1800]);
});

test("rippleCut backwards lengthens the last part; titles and keep stay", () => {
  let p = plan.setTitle(plan.fromDuration(600, 1800), 2, "C");
  p = plan.setKeep(p, 1, false);
  p = plan.rippleCut(p, 1, -3);
  assert.deepEqual(starts(p), [0, 597, 1197]);
  assert.equal(p[2].end_s, 1800);
  assert.equal(p[2].title, "C");
  assert.equal(p[1].keep, false);
});

test("rippleCut clamps against the previous cut and drops cuts past the end", () => {
  const p = plan.fromDuration(600, 1800);
  assert.deepEqual(starts(plan.rippleCut(p, 1, -700)), [0, 0.5, 600.5]);
  const q = plan.rippleCut(p, 1, 700);
  assert.deepEqual(starts(q), [0, 1300]);
  assert.equal(q[1].end_s, 1800);
  assert.equal(plan.rippleCut(p, 0, 5), p);
});

test("nudge ripples only when asked", () => {
  const p = plan.fromDuration(600, 1800);
  assert.deepEqual(starts(plan.nudge(p, 1, 1)), [0, 601, 1200]);
  assert.deepEqual(starts(plan.nudge(p, 1, 1, true)), [0, 601, 1201]);
});

test("parseDuration", () => {
  assert.equal(plan.parseDuration("20:00"), 1200);
  assert.equal(plan.parseDuration("1:02:30"), 3750);
  assert.equal(plan.parseDuration("1200"), 1200);
  assert.equal(plan.parseDuration("20m"), 1200);
  assert.equal(plan.parseDuration("20:30"), 1230);
  assert.equal(plan.parseDuration("abc"), null);
  assert.equal(plan.parseDuration(""), null);
});
