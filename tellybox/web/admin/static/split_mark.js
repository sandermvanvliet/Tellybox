// The geometry of marking a title card (ES-3). Pure: no DOM.
//
// The admin freezes the player's frame on a canvas the size of the <video> element and drags a
// rectangle over it. The video is letterboxed or pillarboxed inside its element (object-fit:
// contain), and the region we store is a fraction (0..1) of the frame itself, not of the element.
// A box is {x, y, w, h} in element pixels; a point is {x, y} in the same pixels.

export const MIN_SIDE = 0.02; // a drag smaller than this (a fraction of the frame) is a tap, not a region

const clamp01 = (v) => Math.min(1, Math.max(0, v));
const round4 = (v) => Math.round(v * 10000) / 10000;

// Where the frame is drawn inside an element of boxW x boxH, for a video of contentW x contentH
// pixels (object-fit: contain). Null when either size is unknown.
export function contentBox(boxW, boxH, contentW, contentH) {
  if (!(boxW > 0 && boxH > 0 && contentW > 0 && contentH > 0)) return null;
  const scale = Math.min(boxW / contentW, boxH / contentH);
  const w = contentW * scale;
  const h = contentH * scale;
  return { x: (boxW - w) / 2, y: (boxH - h) / 2, w, h };
}

// A point in element pixels as fractions of the frame, kept inside it.
export function toNorm(box, point) {
  return { x: clamp01((point.x - box.x) / box.w), y: clamp01((point.y - box.y) / box.h) };
}

// The region [x, y, w, h] (fractions) spanned by a drag from a to b in any direction, clamped to
// the frame; null when it is too small to be meant.
export function dragRegion(box, a, b) {
  if (!box) return null;
  const p = toNorm(box, a);
  const q = toNorm(box, b);
  const x = Math.min(p.x, q.x);
  const y = Math.min(p.y, q.y);
  const w = Math.abs(p.x - q.x);
  const h = Math.abs(p.y - q.y);
  if (w < MIN_SIDE || h < MIN_SIDE) return null;
  return [round4(x), round4(y), round4(Math.min(w, 1 - x)), round4(Math.min(h, 1 - y))];
}

// A region as a rectangle in element pixels, for drawing; null region = the whole frame.
export function regionRect(box, region) {
  const [x, y, w, h] = region || [0, 0, 1, 1];
  return { x: box.x + x * box.w, y: box.y + y * box.h, w: w * box.w, h: h * box.h };
}
