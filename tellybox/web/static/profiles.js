// Who's watching (PR-1, PR-2): the device's remembered pick, the picker and the pictures.
// The kid never reads: kids are big round pictures; names are screen-reader labels only (KA-2).

import { icons, placeholderFace } from "./icons.js";
import { label as tr } from "./i18n.js";

const KEY = "tellybox.who";
export const IDLE_MS = 30 * 60 * 1000; // ask again after 30 minutes without use (PR-2)

// Same distinct, soft colours as the show placeholders.
const COLORS = ["#FF9CB6", "#8C7CF0", "#4FC3C9", "#FFB35C", "#B48CE0", "#6FA8FF", "#F08A6C", "#C98CE0"];
const colorOf = (id) => COLORS[Math.abs(Number(id) || 0) % COLORS.length];

// ---------- the device's pick, kept in localStorage: {profiles, day, last_used} ----------
// Storage can be missing or throw (private windows, blocked site data): every access is guarded,
// and without it the picker simply shows again.

export function readSelection() {
  try {
    const s = JSON.parse(localStorage.getItem(KEY));
    if (!s || !Array.isArray(s.profiles) || !s.profiles.length) return null;
    if (!s.profiles.every((n) => Number.isInteger(n))) return null;
    return { profiles: s.profiles, day: s.day ?? null, last_used: Number(s.last_used) || 0 };
  } catch {
    return null;
  }
}

export function writeSelection(sel) {
  try {
    localStorage.setItem(KEY, JSON.stringify(sel));
  } catch {
    /* not persisted: the picker shows again next time */
  }
}

export function clearSelection() {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* ignore */
  }
}

// The pick still holds: it exists, is from today's timer day, was used within 30 minutes,
// and every kid in it still exists. `day` is null while the server's day isn't known yet.
export function selectionValid(sel, profiles, day, now = Date.now()) {
  if (!sel) return false;
  if (day != null && sel.day != null && sel.day !== day) return false;
  if (now - sel.last_used > IDLE_MS) return false;
  const known = new Set(profiles.map((p) => p.profile_id));
  return sel.profiles.every((id) => known.has(id));
}

// ---------- the group's sky (PR-4): reduced from KidState.profiles ----------
// Lowest fraction left, last five minutes if any member is there, time up if any member is out.
// Falls back to the shared sky when the server doesn't send per-profile state.

export function reduceGroup(state, ids) {
  const members = (ids || []).map((id) => state.profiles?.[String(id)]).filter(Boolean);
  if (!members.length) return { sky: state.sky, time_up: !!state.time_up };
  const fractions = members.filter((m) => !m.unlimited && m.fraction_left != null).map((m) => m.fraction_left);
  const unlimited = members.every((m) => m.unlimited);
  return {
    sky: {
      unlimited,
      fraction_left: unlimited ? null : fractions.length ? Math.min(...fractions) : 1,
      last_five: !unlimited && members.some((m) => m.last_five && !m.unlimited),
    },
    time_up: members.some((m) => m.time_up),
  };
}

// ---------- pictures ----------

// A round picture: the uploaded photo, else the built-in avatar, else a coloured face.
export function pic(profile, cls = "") {
  const box = el("span", `pic ${cls}`.trim());
  const src = profile.picture || (profile.avatar ? `/static/avatars/${encodeURIComponent(profile.avatar)}.svg` : null);
  const fallback = () => {
    box.querySelector("img")?.remove();
    box.insertAdjacentHTML("afterbegin", placeholderFace(colorOf(profile.profile_id)));
  };
  if (!src) {
    fallback();
    return box;
  }
  const img = el("img", "", { alt: "", decoding: "async", draggable: "false" });
  img.addEventListener("error", () => {
    // A missing photo falls back to the avatar, then to the face.
    if (profile.picture && profile.avatar && !img.dataset.tried) {
      img.dataset.tried = "1";
      img.src = `/static/avatars/${encodeURIComponent(profile.avatar)}.svg`;
    } else fallback();
  });
  img.src = src;
  box.append(img);
  return box;
}

function el(tag, cls, attrs = {}) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

// The corner button: the picked kids' pictures, overlapping. Names are the screen-reader label.
// In the reader UI (KA-11) the names and a "Change" label are shown next to the pictures.
export function fillWhoButton(button, profiles, ids, reader = false) {
  const picked = ids.map((id) => profiles.find((p) => p.profile_id === id)).filter(Boolean);
  const shown = picked.slice(0, 4);
  button.dataset.count = String(Math.max(1, shown.length));
  button.replaceChildren(...shown.map((p) => pic(p)));
  if (!shown.length) button.append(pic({ profile_id: 0 }));
  button.classList.toggle("is-reader", reader);
  if (reader) {
    const text = el("span", "who-text");
    text.append(Object.assign(el("span", "who-names"), { textContent: picked.map((p) => p.name).join(", ") }));
    text.append(Object.assign(el("span", "who-change"), { textContent: tr("Change") }));
    button.prepend(text);
  }
  button.setAttribute("aria-label", `${tr("Change who's watching")}: ${picked.map((p) => p.name).join(", ")}`);
}

// ---------- the picker ----------

// Returns {node, update(stateProfiles)}. `onGo(ids)` gets the picked profile ids.
export function renderPicker({ profiles, preselected, stateProfiles, onGo }) {
  const picked = new Set(preselected);
  const root = el("section", "who", { "aria-label": tr("Who's watching?") });
  const grid = el("div", "who-grid", { role: "group", "aria-label": tr("Who's watching?") });
  const goSlot = el("div", "who-go");
  const go = el("button", "big-btn who-go-btn", { type: "button", "aria-label": tr("Go") });
  go.innerHTML = icons.go;
  goSlot.append(go);
  root.append(grid, goSlot);

  const buttons = new Map();
  for (const p of profiles) {
    const b = el("button", "who-kid", { type: "button", "aria-label": p.name || "", "aria-pressed": "false", "data-profile": String(p.profile_id) });
    b.append(pic(p));
    b.append(Object.assign(el("span", "who-check"), { innerHTML: icons.check }));
    b.append(Object.assign(el("span", "who-moon"), { innerHTML: icons.moonBadge }));
    grid.append(b);
    buttons.set(p.profile_id, b);
  }

  let live = stateProfiles || {};
  const isNight = (p) => {
    const s = live[String(p.profile_id)];
    return s ? !!s.time_up : !!p.time_up;
  };

  function paint() {
    for (const p of profiles) {
      const b = buttons.get(p.profile_id);
      const night = isNight(p);
      if (night) picked.delete(p.profile_id); // out of time: can't be picked
      const on = picked.has(p.profile_id);
      b.classList.toggle("is-picked", on);
      b.classList.toggle("is-night", night);
      b.setAttribute("aria-pressed", String(on));
      b.setAttribute("aria-disabled", String(night));
    }
    goSlot.classList.toggle("is-shown", picked.size > 0);
    go.tabIndex = picked.size > 0 ? 0 : -1;
    go.setAttribute("aria-hidden", String(picked.size === 0));
  }

  grid.addEventListener("click", (e) => {
    const b = e.target.closest(".who-kid");
    if (!b) return;
    const id = Number(b.dataset.profile);
    const p = profiles.find((x) => x.profile_id === id);
    if (!p || isNight(p)) return;
    if (picked.has(id)) picked.delete(id);
    else picked.add(id);
    paint();
  });
  go.addEventListener("click", () => {
    if (picked.size) onGo(profiles.map((p) => p.profile_id).filter((id) => picked.has(id)));
  });

  paint();
  return {
    node: root,
    update(next) {
      live = next || {};
      paint();
    },
  };
}
