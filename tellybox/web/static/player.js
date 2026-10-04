// In-app player for the kid app (KA-13, KA-14, PB-7..PB-9, WT-10, WT-11): the video plays on this device
// instead of the TV. Pure helpers come first (no DOM, tested with node in tests/js/player.test.mjs);
// createPlayer() at the bottom holds the DOM, the heartbeats and the wake lock. No text is drawn (KA-2):
// the labels are passed in by app.js, for screen readers only.

import { icons } from "./icons.js";

const DEVICE_KEY = "tellybox.device";
const TARGET_KEY = "tellybox.target";
const ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-";
const ID_PATTERN = /^[A-Za-z0-9_-]{32}$/;
export const HEARTBEAT_MS = 10000;
const BURST_MS = 250;

// ---------- pure helpers ----------

// A random 32-character device id from `getRandomValues` (crypto.getRandomValues); 6 bits per character.
export function makeDeviceId(getRandomValues) {
  const bytes = getRandomValues(new Uint8Array(32));
  let id = "";
  for (const b of bytes) id += ID_ALPHABET[b & 63];
  return id;
}

let memoryId = null; // used when storage throws: the id then lasts for this page only

// The id kept in localStorage (`tellybox.device`); a stored value that isn't well formed is replaced.
export function getDeviceId(storage, getRandomValues) {
  try {
    const stored = storage?.getItem(DEVICE_KEY);
    if (stored && ID_PATTERN.test(stored)) return stored;
  } catch {
    /* storage blocked: fall through */
  }
  if (!memoryId) memoryId = makeDeviceId(getRandomValues);
  try {
    storage?.setItem(DEVICE_KEY, memoryId);
  } catch {
    /* kept in memory */
  }
  return memoryId;
}

// KA-13: the toggle exists only when every picked profile can watch in the app.
export function targetAllowed(profiles, group) {
  if (!group || !group.length) return false;
  return group.every((id) => profiles.some((p) => p.profile_id === id && p.watch_in_app === true));
}

// The saved choice ("tv" by default); "tv" whenever the toggle isn't allowed.
export function readTarget(storage, allowed) {
  if (!allowed) return "tv";
  try {
    return storage?.getItem(TARGET_KEY) === "device" ? "device" : "tv";
  } catch {
    return "tv";
  }
}

export function writeTarget(storage, target) {
  try {
    storage?.setItem(TARGET_KEY, target === "device" ? "device" : "tv");
  } catch {
    /* app.js keeps the choice for this page */
  }
}

// WT-10: the state word for a heartbeat. `v` is {error, ended, paused, waiting, readyState}.
export function heartbeatState(v) {
  if (v.error) return "error";
  if (v.ended) return "ended";
  if (v.paused) return "paused";
  if (v.waiting || v.readyState < 3) return "buffering";
  return "playing";
}

// The heartbeat request body.
export function heartbeatBody(deviceId, v) {
  const body = { device_id: deviceId, state: heartbeatState(v), position_s: Number.isFinite(v.currentTime) ? v.currentTime : 0 };
  if (Number.isFinite(v.duration) && v.duration > 0) body.duration_s = v.duration;
  return body;
}

const TIME_UP_REASONS = new Set(["time_up", "blocked", "stop_now"]);

// What to do with a heartbeat answer: {kind: "continue" | "next" | "time_up" | "library" | "error", next?}.
export function outcomeFor(answer) {
  if (!answer || typeof answer !== "object") return { kind: "continue" };
  if (answer.action === "next") return answer.next && answer.next.url ? { kind: "next", next: answer.next } : { kind: "library" };
  if (answer.action === "stop") {
    if (TIME_UP_REASONS.has(answer.reason)) return { kind: "time_up" };
    if (answer.reason === "error") return { kind: "error" };
    return { kind: "library" };
  }
  return { kind: "continue" }; // includes time_up: true during the grace period: nothing visible changes
}

// ---------- the player ----------

const el = (tag, cls, attrs = {}) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
};

// Opens the full-screen player at once, inside the tap (so fullscreen is allowed), before the play request
// has answered. Returns {start({url, start_s}), abort()}.
//   opts: {parent, deviceId, labels: {exit, back, error}, post(url, body) -> {status, data}, onExit(kind)}
//   onExit(kind) runs once, with "library" | "time_up"; the player is gone by then.
export function createPlayer(opts) {
  const { parent, deviceId, labels, post, onExit } = opts;
  const root = el("div", "player is-loading");
  const video = el("video", "player-video", { playsinline: "", "webkit-playsinline": "", controls: "", autoplay: "", preload: "auto" });
  const exitBtn = el("button", "player-exit", { type: "button", "aria-label": labels.exit });
  exitBtn.innerHTML = icons.close;
  root.append(video, Object.assign(el("div", "player-wait", { "aria-hidden": "true" }), { innerHTML: icons.spinner }), exitBtn);
  parent.append(root);

  let closed = false;
  let started = false;
  let startAt = 0;
  let waiting = false;
  let errored = false;
  let inflight = false;
  let again = false;
  let burst = null;
  let wake = null;
  let fsEntered = false;
  const listeners = [];

  const requestFs = root.requestFullscreen || root.webkitRequestFullscreen;
  const fsElement = () => document.fullscreenElement || document.webkitFullscreenElement || null;
  try {
    requestFs?.call(root)?.catch?.(() => {});
  } catch {
    /* stays an inline, viewport-filling player */
  }

  const view = () => ({ error: errored, ended: video.ended, paused: video.paused, waiting, readyState: video.readyState, currentTime: video.currentTime, duration: video.duration });

  // ----- wake lock (best effort)
  async function wantWake(on) {
    try {
      if (on && !wake && navigator.wakeLock && document.visibilityState === "visible") {
        const lock = await navigator.wakeLock.request("screen");
        wake = lock;
        lock.addEventListener?.("release", () => {
          if (wake === lock) wake = null;
        });
      } else if (!on && wake) {
        const lock = wake;
        wake = null;
        await lock.release();
      }
    } catch {
      /* not available or refused */
    }
  }

  function teardown() {
    closed = true;
    clearInterval(timer);
    clearTimeout(burst);
    for (const [target, name, fn] of listeners) target.removeEventListener(name, fn);
    wantWake(false);
    try {
      if (fsElement()) document.exitFullscreen?.()?.catch?.(() => {}) ?? document.webkitExitFullscreen?.();
    } catch {
      /* ignore */
    }
    root.remove();
  }

  // ----- leaving
  function finish(kind, { stop = false } = {}) {
    if (closed) return;
    if (stop) post("/api/kid/device/stop", { device_id: deviceId });
    teardown(); // listeners go first, so emptying the video below raises nothing
    try {
      video.pause();
      video.removeAttribute("src");
      video.load();
    } catch {
      /* ignore */
    }
    onExit(kind);
  }

  // The child ends it (exit icon, leaving fullscreen): tell the server.
  const leave = () => finish("library", { stop: true });

  function showError() {
    if (closed || root.classList.contains("is-error")) return;
    errored = true;
    clearInterval(timer);
    clearTimeout(burst);
    wantWake(false);
    try {
      video.pause();
    } catch {
      /* ignore */
    }
    root.classList.add("is-error");
    const back = el("button", "player-back", { type: "button", "aria-label": labels.back });
    back.innerHTML = icons.home;
    back.addEventListener("click", leave);
    root.replaceChildren(Object.assign(el("div", "player-sad", { role: "img", "aria-label": labels.error }), { innerHTML: icons.sadCloud }), back);
  }

  // ----- heartbeats (WT-10)
  async function beat() {
    if (closed || errored || !started) return;
    if (inflight) {
      again = true; // never overlap requests
      return;
    }
    inflight = true;
    const { status, data } = await post("/api/kid/device/heartbeat", heartbeatBody(deviceId, view()));
    inflight = false;
    if (closed) return;
    if (status === 200) apply(outcomeFor(data));
    // anything else (network failure, 5xx): the next beat tries again; the server ends the session after 5 min
    if (again && !closed) {
      again = false;
      beat();
    }
  }

  function apply(out) {
    if (out.kind === "next") {
      startAt = Number(out.next.start_s) || 0;
      waiting = false;
      video.src = out.next.url;
      video.play?.()?.catch?.(() => {});
    } else if (out.kind === "time_up" || out.kind === "library") finish(out.kind);
    else if (out.kind === "error") showError();
  }

  const soon = () => {
    clearTimeout(burst);
    burst = setTimeout(beat, BURST_MS);
  };

  const timer = setInterval(beat, HEARTBEAT_MS);
  const on = (target, name, fn) => {
    target.addEventListener(name, fn);
    listeners.push([target, name, fn]);
  };

  on(video, "loadedmetadata", () => {
    if (startAt > 0) {
      try {
        video.currentTime = startAt;
      } catch {
        /* ignore */
      }
    }
    if (!requestFs && video.webkitSupportsFullscreen && !video.webkitDisplayingFullscreen) {
      try {
        video.webkitEnterFullscreen(); // iPhone Safari: no element fullscreen, only the video's own
      } catch {
        /* stays inline */
      }
    }
  });
  for (const name of ["play", "pause", "seeked", "playing", "ended"]) {
    on(video, name, () => {
      if (name === "playing") {
        waiting = false;
        root.classList.remove("is-loading");
        wantWake(true);
      } else if (name === "pause" || name === "ended") wantWake(false);
      soon();
    });
  }
  on(video, "waiting", () => {
    waiting = true;
    root.classList.add("is-loading");
    soon();
  });
  on(video, "canplay", () => root.classList.remove("is-loading"));
  on(video, "error", () => {
    if (!started) return;
    errored = true;
    post("/api/kid/device/heartbeat", heartbeatBody(deviceId, view())); // tell the server; its answer no longer matters
    showError();
  });
  on(video, "webkitendfullscreen", leave);
  const onFsChange = () => {
    if (fsElement() === root) fsEntered = true;
    else if (fsEntered && !fsElement()) leave();
  };
  on(document, "fullscreenchange", onFsChange);
  on(document, "webkitfullscreenchange", onFsChange);
  on(document, "visibilitychange", () => {
    if (document.visibilityState === "visible" && !video.paused) wantWake(true);
  });
  on(window, "pagehide", () => {
    if (closed) return;
    try {
      navigator.sendBeacon("/api/kid/device/stop", new Blob([JSON.stringify({ device_id: deviceId })], { type: "application/json" }));
    } catch {
      /* best effort */
    }
  });
  exitBtn.addEventListener("click", leave);

  return {
    // The play request answered: begin the video at `start_s`.
    start({ url, start_s }) {
      if (closed) return;
      started = true;
      startAt = Number(start_s) || 0;
      video.src = url;
      video.play?.()?.catch?.(() => {});
    },
    // The play request failed for a reason that isn't the kid's pick: show the sad cloud and the way back.
    fail: showError,
    // The play request was refused: remove the player; nothing started, so the server is not told.
    abort() {
      if (!closed) teardown();
    },
  };
}
