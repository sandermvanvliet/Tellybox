// The sky is the timer (KA-8, KA-9). Pure mapping from KidState to the scene.

import { label as tr } from "./i18n.js";

const clamp01 = (v) => Math.max(0, Math.min(1, v));

// Sun centre in % of the sky band. High at 1.0, sinking along a gentle arc toward the
// far hills as time runs out; fully behind them when time is up.
export function sunPosition(sky, timeUp) {
  if (timeUp) return { x: 80, y: 140 };
  if (!sky || sky.unlimited || sky.fraction_left == null) return { x: 50, y: 30 };
  const gone = 1 - clamp01(sky.fraction_left);
  return {
    x: 24 + gone * 54,
    y: 30 + 56 * Math.pow(gone, 1.35), // 30% (high) .. 86% (half behind the far hills)
  };
}

export function phase(sky, timeUp) {
  if (timeUp) return "night";
  if (sky && sky.unlimited) return "unlimited";
  if (sky && sky.last_five) return "dusk";
  return "day";
}

export function applySky(state, { body, sun, skyEl }) {
  const p = phase(state.sky, state.time_up);
  for (const c of ["day", "dusk", "night", "unlimited"]) body.classList.toggle(`is-${c}`, c === p);
  const { x, y } = sunPosition(state.sky, state.time_up);
  sun.style.setProperty("--sun-x", `${x}%`);
  sun.style.setProperty("--sun-y", `${y}%`);

  // Screen readers only: the same meaning the sky shows.
  let label;
  if (p === "night") label = tr("Time's up for today");
  else if (p === "unlimited") label = tr("No time limit");
  else {
    const pct = Math.round(clamp01(state.sky?.fraction_left ?? 1) * 100);
    label = p === "dusk"
      ? tr("%(pct)s% of today's time left, almost done", { pct })
      : tr("%(pct)s% of today's time left", { pct });
  }
  skyEl.setAttribute("aria-label", label);

  const theme = { day: "#8FD3FF", unlimited: "#8FD3FF", dusk: "#FF8A5B", night: "#232B63" }[p];
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme);
}
