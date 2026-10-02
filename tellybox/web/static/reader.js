// Reader UI helpers (KA-11, KA-12): pure functions, no DOM. The reader UI adds text to the
// picture UI; it never replaces it.

import { label as tr } from "./i18n.js";

// KA-11: the reader UI only when every picked profile is a reader. An empty pick, an unknown
// profile or a single "icons" profile means the icon UI, so a child who can't read never gets a
// text-dependent screen.
export function isReaderGroup(profiles, ids) {
  if (!ids || !ids.length) return false;
  return ids.every((id) => profiles.find((p) => p.profile_id === id)?.ui_mode === "text");
}

// KA-12: case- and accent-insensitive substring match; an empty query matches everything.
const fold = (s) => String(s || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
export function matchesQuery(title, query) {
  const q = fold(query).trim();
  return !q || fold(title).includes(q);
}

// "Playing" / "Paused" / "Loading" for the now-playing state.
export function statusText(npState) {
  if (npState === "paused") return tr("Paused");
  if (npState === "playing") return tr("Playing");
  return tr("Loading");
}

// Today's time left as text, from the sky the device shows (the group's, reduced).
export function timeLeftText(sky, timeUp) {
  if (timeUp) return tr("Time's up for today");
  if (!sky || sky.unlimited) return tr("No time limit today");
  const pct = Math.round(Math.max(0, Math.min(1, sky.fraction_left ?? 1)) * 100);
  return tr("%(pct)s% of today's time left", { pct });
}
