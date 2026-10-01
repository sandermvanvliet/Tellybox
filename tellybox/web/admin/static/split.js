// Split editor (ES-1, ES-2, ES-7): cut one source video into episodes (split.html).
//
// The page serves the state as JSON in <script id="split-state"> (the same as GET
// /admin/api/splits/{id}); the plan is edited here and autosaved with PUT. The plan logic
// is in split_plan.js. Without this script the page shows its notice and the approve form
// can't be used, since there is no plan to save.

import { t, tn } from "./i18n.js";
import * as P from "./split_plan.js";

const SAVE_DELAY_MS = 800;
const FRAME_DELAY_MS = 400;
const POLL_MS = 2000;

// problems() codes (split_plan.js) as messages; the same texts as splitting.py.
const PROBLEMS = {
  empty: () => t("The plan has no parts."),
  too_many: () => t("A video can be split into at most %(n)d parts.", { n: P.MAX_SEGMENTS }),
  start: () => t("The first part must start at the beginning of the video."),
  end: () => t("The last part must end at the end of the video."),
  gap: () => t("Parts must follow each other without gaps."),
  title: () => t("Titles can be at most %(n)d characters.", { n: P.MAX_TITLE }),
  short: () => t("Each part must be at least %(n)d seconds long.", { n: P.MIN_KEPT_S }),
  none: () => t("Keep at least one part."),
  single: () => t("Add a cut, or leave out a part, before approving."),
};

const root = document.getElementById("split-editor");
const stateEl = document.getElementById("split-state");

function pad(n, width = 2) {
  return String(n).padStart(width, "0");
}

// m:ss.cc, or h:mm:ss.cc from an hour on.
function fmtTime(seconds) {
  const cs = Math.floor(Math.max(0, seconds) * 100 + 1e-6);
  const h = Math.floor(cs / 360000);
  const m = Math.floor((cs % 360000) / 6000);
  const s = Math.floor((cs % 6000) / 100);
  const frac = pad(cs % 100);
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}.${frac}` : `${m}:${pad(s)}.${frac}`;
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  node.append(...children);
  return node;
}

function init() {
  let state;
  try {
    state = JSON.parse(stateEl.textContent);
  } catch {
    return;
  }
  const q = (selector) => root.querySelector(selector);
  const video = q("[data-split-video]");
  const timeOut = q("[data-split-time]");
  const listEl = q("[data-split-segments]");
  const stripEl = q("[data-split-strip]");
  const statusEl = q("[data-split-status]");
  const approve = q("[data-split-approve]") || document.querySelector("[data-split-approve]");
  const estimate = document.querySelector("[data-split-estimate]");
  const cutButton = q('[data-action="cut"]');
  const chaptersButton = q('[data-action="use-chapters"]');
  const api = root.dataset.api || `/admin/api/splits/${state.source_id}`;
  if (!video || !listEl || !stripEl) {
    // No editor on this page (the original video is gone): only follow a running split job.
    if (["approved", "cutting"].includes(state.status)) poll();
    return;
  }
  const fps = state.fps > 0 ? state.fps : 25;
  const duration = state.duration_s;

  let plan = state.segments.map((s) => ({ start_s: P.ms(s.start_s), end_s: P.ms(s.end_s), title: s.title || "", keep: s.keep !== false }));
  let origin = state.origin;
  let readOnly = root.dataset.editable === "false" || state.editable === false || state.has_file === false;
  let dirty = false;
  let saveTimer = null;
  let chain = Promise.resolve();
  let saveError = null;
  let current = -1;

  // Frame numbers: a displayed frame k covers [k/fps, (k+1)/fps), and we seek a little inside it.
  const frameOf = (time) => Math.floor(time * fps + 1e-3);
  const timeOfFrame = (k) => (k + 0.1) / fps;
  const snap = (time) => P.ms(frameOf(time) / fps);

  function setStatus(text, kind = "") {
    if (!statusEl) return;
    statusEl.textContent = text;
    statusEl.className = kind;
  }

  // ---- player ----

  function seek(time) {
    video.currentTime = Math.min(Math.max(0, time), Math.max(0, duration - 0.01));
    showTime();
  }

  function stepFrames(n) {
    video.pause();
    seek(timeOfFrame(frameOf(video.currentTime) + n));
  }

  function step(value) {
    if (value === "-frame") stepFrames(-1);
    else if (value === "+frame") stepFrames(1);
    else {
      video.pause();
      seek(video.currentTime + Number(value));
    }
  }

  function showTime() {
    const now = video.currentTime || 0;
    if (timeOut) timeOut.textContent = fmtTime(now);
    const i = P.indexAt(plan, now);
    if (i !== current) {
      current = i;
      markCurrent();
    }
  }

  function markCurrent() {
    [...listEl.children].forEach((row, i) => row.classList.toggle("current", i === current));
    [...stripEl.children].forEach((card, i) => card.classList.toggle("current", i === current));
  }

  function tick() {
    showTime();
    if (!video.paused && !video.ended) requestAnimationFrame(tick);
  }

  // ---- plan edits ----

  function edit(next) {
    if (next === plan || readOnly) return;
    plan = next;
    if (origin !== "manual") origin = "manual";
    dirty = true;
    render();
    scheduleSave();
  }

  function cutHere() {
    if (readOnly) return;
    const time = snap(video.currentTime);
    const next = P.addCut(plan, time);
    if (next === plan) setStatus(t("A cut needs at least half a second on both sides."), "error");
    else edit(next);
  }

  function useChapters() {
    if (readOnly || !state.chapters?.length) return;
    if (plan.length > 1 && !confirm(t("Replace your cuts with the video's chapters?"))) return;
    plan = P.fromChapters(state.chapters, duration);
    origin = "chapters";
    dirty = true;
    render();
    scheduleSave();
  }

  // ---- saving ----

  function scheduleSave() {
    clearTimeout(saveTimer);
    setStatus(t("Saving…"));
    saveTimer = setTimeout(() => (chain = chain.then(save)), SAVE_DELAY_MS);
  }

  // Resolves when everything edited so far is saved (or has failed; see saveError).
  function flush() {
    clearTimeout(saveTimer);
    chain = chain.then(save);
    return chain;
  }

  async function save() {
    if (!dirty) return;
    dirty = false;
    saveError = null;
    setStatus(t("Saving…"));
    try {
      const res = await fetch(api, {
        method: "PUT",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ segments: plan, origin }),
      });
      if (res.status === 409) {
        await conflict((await res.json().catch(() => ({}))).error);
      } else if (!res.ok) {
        saveError = (await res.json().catch(() => ({}))).error || t("Saving failed. Your changes are not saved yet.");
        dirty = true; // try again with the next edit
        setStatus(saveError, "error");
      } else if (!dirty) {
        setStatus(t("Saved"), "ok");
      }
    } catch {
      saveError = t("Saving failed. Your changes are not saved yet.");
      dirty = true;
      setStatus(saveError, "error");
    }
  }

  // Someone else changed the proposal (a split job started): show what is there, read-only.
  async function conflict(message) {
    readOnly = true;
    dirty = false;
    clearTimeout(saveTimer);
    try {
      const res = await fetch(api, { headers: { Accept: "application/json" } });
      if (res.ok) {
        state = await res.json();
        plan = state.segments.map((s) => ({ ...s, title: s.title || "" }));
      }
    } catch {
      // keep what we have
    }
    render();
    setStatus(message || t("This plan can't be changed any more."), "error");
    if (["approved", "cutting"].includes(state.status)) poll();
  }

  window.addEventListener("pagehide", () => {
    if (!dirty || readOnly) return;
    fetch(api, {
      method: "PUT",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ segments: plan, origin }),
    }).catch(() => {});
  });

  // ---- rendering ----

  function render() {
    renderList();
    renderStrip();
    renderApprove();
    markCurrent();
  }

  function renderList() {
    if (listEl.children.length !== plan.length) {
      listEl.replaceChildren(...plan.map((_, i) => makeRow(i)));
    }
    plan.forEach((s, i) => {
      const row = listEl.children[i];
      row.dataset.i = i;
      row.classList.toggle("left-out", !s.keep);
      row.querySelector(".seg-name").textContent = t("Part %(n)d", { n: i + 1 });
      row.querySelector(".seg-range").textContent = `${fmtTime(s.start_s)} – ${fmtTime(s.end_s)}`;
      const title = row.querySelector("input.seg-title");
      if (title.value !== s.title) title.value = s.title;
      title.disabled = readOnly;
      const keep = row.querySelector(".seg-keep");
      keep.textContent = s.keep ? t("Keep") : t("Left out");
      keep.setAttribute("aria-pressed", String(s.keep));
      keep.disabled = readOnly;
      const cut = row.querySelector(".seg-cut");
      if (cut) {
        row.querySelector(".seg-cut-time").textContent = t("Cut at %(time)s", { time: fmtTime(s.start_s) });
        cut.querySelectorAll("button[data-nudge], button[data-remove]").forEach((b) => (b.disabled = readOnly));
      }
    });
  }

  function makeRow(i) {
    const index = (event) => Number(event.target.closest("li").dataset.i);
    const nudgeButton = (delta, label) =>
      el("button", {
        type: "button", class: "small", "data-nudge": delta, text: label,
        "aria-label": delta < 0
          ? t("Move the cut back by %(amount)s s", { amount: -delta })
          : t("Move the cut forward by %(amount)s s", { amount: delta }),
        onclick: (event) => edit(P.nudge(plan, index(event), delta)),
      });
    const row = el(
      "li",
      { class: "seg" },
      el(
        "div",
        { class: "seg-head" },
        el("strong", { class: "seg-name" }),
        el("span", { class: "seg-range muted" }),
        el("button", {
          type: "button", class: "small seg-keep",
          onclick: (event) => {
            const k = index(event);
            edit(P.setKeep(plan, k, !plan[k].keep));
          },
        }),
      ),
      el("input", {
        type: "text", class: "seg-title", maxlength: P.MAX_TITLE, placeholder: t("Title (optional)"), "aria-label": t("Title"),
        oninput: (event) => {
          if (readOnly) return;
          plan = P.setTitle(plan, index(event), event.target.value);
          origin = "manual";
          dirty = true;
          renderApprove();
          scheduleSave();
        },
      }),
      el(
        "div",
        { class: "seg-tools" },
        el("button", { type: "button", class: "small", text: t("Go to"), onclick: (event) => seek(plan[index(event)].start_s) }),
      ),
    );
    if (i > 0) {
      row.querySelector(".seg-tools").append(
        el(
          "span",
          { class: "seg-cut" },
          el("span", { class: "seg-cut-time muted" }),
          nudgeButton(-1, "−1 s"),
          nudgeButton(-0.1, "−0.1 s"),
          nudgeButton(0.1, "+0.1 s"),
          nudgeButton(1, "+1 s"),
          el("button", {
            type: "button", class: "small danger", "data-remove": "", text: t("Remove cut"),
            onclick: (event) => edit(P.removeCut(plan, index(event))),
          }),
        ),
      );
    }
    return row;
  }

  // ES-7: the frame at the start of every part, to find the cuts by eye.
  const frameTimers = new Map();

  function frameSrc(time) {
    return `${state.frame_url}?t=${Math.min(time, Math.max(0, duration - 0.1))}`;
  }

  function renderStrip() {
    if (stripEl.children.length !== plan.length) {
      frameTimers.forEach(clearTimeout);
      frameTimers.clear();
      stripEl.replaceChildren(...plan.map((s, i) => makeCard(i, s)));
    }
    plan.forEach((s, i) => {
      const card = stripEl.children[i];
      card.dataset.i = i;
      card.classList.toggle("left-out", !s.keep);
      card.querySelector(".card-time").textContent = fmtTime(s.start_s);
      card.setAttribute("aria-label", t("Go to part %(n)d at %(time)s", { n: i + 1, time: fmtTime(s.start_s) }));
      card.querySelector(".card-n").textContent = String(i + 1);
      const img = card.querySelector("img");
      if (Number(card.dataset.t) !== s.start_s) {
        card.dataset.t = s.start_s;
        clearTimeout(frameTimers.get(card));
        frameTimers.set(card, setTimeout(() => (img.src = frameSrc(s.start_s)), FRAME_DELAY_MS));
      }
    });
  }

  function makeCard(i, s) {
    const img = el("img", { class: "thumb", alt: "", loading: "lazy", src: frameSrc(s.start_s) });
    const card = el(
      "button",
      { type: "button", class: "strip-card", onclick: (event) => seek(plan[Number(event.currentTarget.dataset.i)].start_s) },
      img,
      el("span", { class: "card-n" }),
      el("span", { class: "card-time" }),
    );
    card.dataset.t = s.start_s;
    return card;
  }

  function renderApprove() {
    const found = P.problems(plan, duration);
    const message = found.map((code) => PROBLEMS[code]()).join(" ");
    if (approve) {
      const submit = approve.querySelector('[type="submit"]');
      if (submit) {
        submit.setAttribute("aria-disabled", String(found.length > 0));
        submit.classList.toggle("blocked", found.length > 0);
        submit.title = message;
      }
      approve.dataset.problems = message;
    }
    if (estimate) {
      const minutes = Math.max(1, Math.ceil(P.keptDuration(plan) / 60));
      estimate.textContent = tn("Cutting takes up to about %(num)d minute", "Cutting takes up to about %(num)d minutes", minutes);
    }
    if (cutButton) cutButton.disabled = readOnly;
    if (chaptersButton) chaptersButton.disabled = readOnly;
  }

  // ---- wiring ----

  root.querySelectorAll("[data-step]").forEach((button) => button.addEventListener("click", () => step(button.dataset.step)));
  if (cutButton) cutButton.addEventListener("click", cutHere);
  if (chaptersButton) chaptersButton.addEventListener("click", useChapters);
  for (const type of ["timeupdate", "seeking", "seeked", "loadedmetadata"]) video.addEventListener(type, showTime);
  video.addEventListener("play", () => requestAnimationFrame(tick));

  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (target.closest?.("input, textarea, select, [contenteditable]")) return;
    const big = event.shiftKey ? 10 : 1;
    switch (event.key) {
      case "ArrowLeft": step(String(-big)); break;
      case "ArrowRight": step(String(big)); break;
      case ",": stepFrames(-1); break;
      case ".": stepFrames(1); break;
      case "c": case "C": cutHere(); break;
      case " ":
        // On a button or the player itself, Space already does its own thing.
        if (target.closest?.("button, a, video")) return;
        if (video.paused) video.play().catch(() => {});
        else video.pause();
        break;
      default: return;
    }
    event.preventDefault();
  });

  if (approve) {
    approve.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!readOnly) {
        await flush();
        const found = P.problems(plan, duration);
        if (found.length || saveError) {
          setStatus(found.length ? found.map((code) => PROBLEMS[code]()).join(" ") : saveError, "error");
          return;
        }
      }
      approve.submit();
    });
  }

  // ---- read-only: follow the split job ----

  async function poll() {
    let next = null;
    try {
      const res = await fetch(api, { headers: { Accept: "application/json" } });
      if (res.ok) next = await res.json();
    } catch {
      // the web service is restarting; try again
    }
    if (next) {
      state = next;
      if (!["approved", "cutting"].includes(next.status)) {
        location.reload();
        return;
      }
      const bar = document.querySelector("[data-split-progress] > span");
      if (bar) bar.style.width = `${Math.round((next.job?.progress ?? 0) * 100)}%`;
    }
    setTimeout(poll, POLL_MS);
  }

  render();
  showTime();
  if (readOnly && ["approved", "cutting"].includes(state.status)) poll();
}

if (root && stateEl) init();
