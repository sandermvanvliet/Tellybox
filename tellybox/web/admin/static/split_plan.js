// The cut plan of one source video (ES-1, ES-2), as the split editor edits it. Pure: no DOM.
//
// A plan is a list of contiguous segments {start_s, end_s, title, keep} from 0 to the video's
// duration, on the timeline of the file on disk. Every function returns a new plan (the same
// object when it refuses to change anything). The rules mirror tellybox/splitting.py.

export const MIN_GAP_S = 0.5; // a cut keeps this distance from other cuts and from the edges
export const MIN_KEPT_S = 5.0;
export const MIN_DROPPED_S = 0.1;
export const MAX_TITLE = 200;
export const MAX_SEGMENTS = 200;
export const END_TOLERANCE_S = 1.0;
const EPS = 1e-3;

export const ms = (value) => Math.round(Number(value) * 1000) / 1000;

const seg = (start_s, end_s, title = "", keep = true) => ({ start_s: ms(start_s), end_s: ms(end_s), title, keep });

export function whole(duration, title = "") {
  return [seg(0, duration, title)];
}

// The cut points (every start but the first).
export function cuts(plan) {
  return plan.slice(1).map((s) => s.start_s);
}

// The index of the segment containing time t (the last one for t at or after the end).
export function indexAt(plan, t) {
  for (let i = plan.length - 1; i > 0; i--) if (t >= plan[i].start_s) return i;
  return 0;
}

// Splits the segment containing t. The new right part has no title and the left part's keep.
export function addCut(plan, t) {
  t = ms(t);
  const i = indexAt(plan, t);
  const s = plan[i];
  if (t - s.start_s < MIN_GAP_S - EPS || s.end_s - t < MIN_GAP_S - EPS) return plan;
  return [...plan.slice(0, i), seg(s.start_s, t, s.title, s.keep), seg(t, s.end_s, "", s.keep), ...plan.slice(i + 1)];
}

// Removes the cut at the start of segment i (i >= 1): merges it into the segment before, which keeps its title.
export function removeCut(plan, i) {
  if (i < 1 || i >= plan.length) return plan;
  const left = plan[i - 1];
  return [...plan.slice(0, i - 1), seg(left.start_s, plan[i].end_s, left.title, left.keep), ...plan.slice(i + 1)];
}

// Moves the cut at the start of segment i to t, but not closer than MIN_GAP_S to its neighbours.
export function moveCut(plan, i, t) {
  if (i < 1 || i >= plan.length) return plan;
  const lo = plan[i - 1].start_s + MIN_GAP_S;
  const hi = plan[i].end_s - MIN_GAP_S;
  if (lo > hi) return plan;
  t = ms(Math.min(hi, Math.max(lo, t)));
  if (t === plan[i].start_s) return plan;
  return plan.map((s, j) => (j === i - 1 ? seg(s.start_s, t, s.title, s.keep) : j === i ? seg(t, s.end_s, s.title, s.keep) : s));
}

export function nudge(plan, i, delta) {
  return i >= 1 && i < plan.length ? moveCut(plan, i, plan[i].start_s + delta) : plan;
}

export function setTitle(plan, i, title) {
  return plan.map((s, j) => (j === i ? { ...s, title } : s));
}

export function setKeep(plan, i, keep) {
  return plan.map((s, j) => (j === i ? { ...s, keep: Boolean(keep) } : s));
}

// ES-1: one segment per chapter, named after it. Only the chapter starts count, like
// splitting.segments_from_chapters. Chapters are {start_s, end_s, title}.
export function fromChapters(chapters, duration) {
  const titles = new Map(chapters.map((c) => [ms(c.start_s), String(c.title || "").trim()]));
  const points = [...new Set(chapters.map((c) => c.start_s).filter((c) => c > EPS && c < duration - EPS).map(ms))].sort((a, b) => a - b);
  const bounds = [0, ...points, ms(duration)];
  return bounds.slice(0, -1).map((start, i) => seg(start, bounds[i + 1], titles.get(start) ?? ""));
}

export function keptDuration(plan) {
  return ms(plan.filter((s) => s.keep).reduce((sum, s) => sum + (s.end_s - s.start_s), 0));
}

// What stops the plan from being approved, as codes (split.js turns them into messages), like
// splitting.validate: the shape errors first, and only when the shape is fine the rest.
export function problems(plan, duration) {
  if (!plan.length) return ["empty"];
  const found = [];
  const add = (code) => found.includes(code) || found.push(code);
  if (plan.length > MAX_SEGMENTS) add("too_many");
  if (Math.abs(plan[0].start_s) > EPS) add("start");
  if (Math.abs(plan[plan.length - 1].end_s - duration) > END_TOLERANCE_S) add("end");
  plan.forEach((s, i) => {
    if (i > 0 && Math.abs(s.start_s - plan[i - 1].end_s) > EPS) add("gap");
    if (s.end_s - s.start_s <= 0) add("gap");
    if ((s.title || "").trim().length > MAX_TITLE) add("title");
  });
  if (found.length) return found;
  if (plan.some((s) => s.end_s - s.start_s < (s.keep ? MIN_KEPT_S : MIN_DROPPED_S))) add("short");
  if (!plan.some((s) => s.keep)) add("none");
  else if (plan.length === 1) add("single");
  return found;
}
