// Tellybox kid app (KA-1..KA-10). Vanilla ES modules, no build step.
// Routes: #/ (home), #/show/{id} and #/who (who is watching, PR-2). Live state via SSE; the sky is the timer.

import { api, subscribe, HttpError } from "./api.js";
import { icons, placeholderTv } from "./icons.js";
import { applySky } from "./sky.js";
import { label as tr, translatePage } from "./i18n.js";
import { isReaderGroup, matchesQuery, statusText, timeLeftText } from "./reader.js";
import { readSelection, writeSelection, clearSelection, selectionValid, reduceGroup, fillWhoButton, renderPicker } from "./profiles.js";

translatePage(); // NF-13: <html lang> and the screen-reader labels in index.html

const $ = (sel, root = document) => root.querySelector(sel);
const view = $("#view");
const nowbar = $("#nowbar");
const skyParts = { body: document.body, sun: $("#sun"), skyEl: $("#sky") };
const whoBtn = $("#whobtn");

// ---------- state ----------

let state = {
  tv: "ok",
  now_playing: null,
  sky: { fraction_left: 1, last_five: false, unlimited: false },
  time_up: false,
  profiles: {}, // per kid: {fraction_left, last_five, unlimited, time_up}
  watching: [],
  day: null, // the timer day; a new day means asking who's watching again
};
let allProfiles = []; // [{profile_id, name, picture, avatar, ...}] in the admin's order
let group = []; // the profile ids this device is watching as (PR-2)
let picker = null; // the who's-watching screen while it is showing
let lastTouch = 0;
let streamDown = false; // SSE errored and no event since: treat the TV as unreachable
let playBusy = false; // one pick request in flight
let toggleBusy = false; // one pause/resume request in flight
let route = null;
let reader = false; // KA-11: the reader UI (text added) for this device's pick
let searchQuery = ""; // KA-12: the home search box, kept across live refreshes
let viewToken = 0;
let cameFromHome = false;

// KA-11: the reader UI only when every picked profile is a reader (never on the picker).
function computeReader() {
  return route?.name !== "who" && isReaderGroup(allProfiles, group);
}

const tvDown = () => streamDown || state.tv !== "ok";

// Distinct, soft colours for placeholders (the API has no show colour).
const SHOW_COLORS = ["#FF9CB6", "#8C7CF0", "#4FC3C9", "#FFB35C", "#B48CE0", "#6FA8FF", "#F08A6C", "#C98CE0"];
const showColor = (id) => SHOW_COLORS[Math.abs(Number(id) || 0) % SHOW_COLORS.length];

// ---------- small DOM helpers ----------

function el(tag, cls, attrs = {}) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}

// 16:9 media box with ink outline; a drawn TV replaces a missing image.
function mediaBox(src, color, cls = "") {
  const box = el("span", `media ${cls}`.trim());
  box.style.setProperty("--c", color);
  const fallback = () => {
    box.classList.add("is-missing");
    box.querySelector("img")?.remove();
    box.insertAdjacentHTML("afterbegin", placeholderTv(color));
  };
  if (!src) {
    fallback();
    return box;
  }
  const img = el("img", "", { alt: "", loading: "lazy", decoding: "async", draggable: "false" });
  img.addEventListener("load", () => box.classList.add("is-loaded"), { once: true });
  img.addEventListener("error", fallback, { once: true });
  img.src = src;
  box.append(img);
  return box;
}

function episodeTile(t, kind) {
  const b = el("button", "tile tile-ep", { type: "button", "aria-label": t.title, "data-ep": String(t.episode_id) });
  const media = mediaBox(t.thumb, showColor(t.show_id));
  b.append(media);
  if (kind === "next") {
    b.append(Object.assign(el("span", "badge"), { innerHTML: icons.next }));
  } else if (t.finished) {
    b.append(Object.assign(el("span", "badge"), { innerHTML: icons.star }));
  } else if (t.progress != null && t.progress > 0) {
    const stripe = el("span", "stripe");
    const fill = el("span");
    fill.style.width = `${Math.max(6, Math.min(100, t.progress * 100))}%`;
    stripe.append(fill);
    media.append(stripe);
  }
  b.append(caption(t.title));
  return b;
}

// KA-10: a short title under the picture, for adults. aria-hidden because the tile's aria-label
// already carries it. Always present (even when empty) so it reserves its two lines and
// tiles in a row line up. KA-11: in the reader UI it is the visible title, read by screen readers
// like any text, and bigger.
function caption(title) {
  const c = el("span", reader ? "caption caption-text" : "caption", reader ? {} : { "aria-hidden": "true" });
  c.textContent = title || "";
  return c;
}

function showTile(s) {
  const a = el("a", "tile tile-show", { href: `#/show/${s.show_id}`, "aria-label": s.title, "data-show": String(s.show_id) });
  a.style.setProperty("--c", showColor(s.show_id));
  // The artwork and the card stacked behind it, so they dim together at night.
  const stack = el("span", "stack");
  stack.append(mediaBox(s.artwork, showColor(s.show_id)));
  a.append(stack, caption(s.title));
  return a;
}

// ---------- views ----------

function parseRoute() {
  if (/^#\/who\/?$/.test(location.hash)) return { name: "who" };
  const m = location.hash.match(/^#\/show\/(\d+)\/?$/);
  if (m) return { name: "show", id: Number(m[1]) };
  return { name: "home" };
}

async function loadView({ keepPlace = false } = {}) {
  const token = ++viewToken;
  const r = route;
  let data;
  try {
    if (r.name === "who") data = await api.profiles();
    else data = r.name === "show" ? await api.show(r.id, group) : await api.home(group);
  } catch (err) {
    if (token !== viewToken) return;
    if (err instanceof HttpError && err.status === 400 && r.name !== "who") {
      forgetGroup(); // a kid in the pick is gone: ask again
      return;
    }
    if (err instanceof HttpError && err.status === 404 && r.name === "show") {
      history.replaceState(null, "", "#/"); // show hidden or gone: back home
      onRoute();
      return;
    }
    setTimeout(() => token === viewToken && loadView({ keepPlace }), 3000);
    return;
  }
  if (token !== viewToken) return;

  // Keep scroll positions and focus across live refreshes.
  const scrollTop = view.scrollTop;
  const strip = $(".strip", view);
  const stripLeft = strip ? strip.scrollLeft : 0;
  const focused = document.activeElement && view.contains(document.activeElement) ? document.activeElement : null;
  const keepSearch = focused?.classList.contains("search-input") ? { start: focused.selectionStart, end: focused.selectionEnd } : null;
  const focusKey = focused?.classList.contains("search-input") ? ".search-input" : focused?.dataset.ep ? `[data-ep="${focused.dataset.ep}"]` : focused?.dataset.show ? `[data-show="${focused.dataset.show}"]` : focused?.classList.contains("btn-home") ? ".btn-home" : null;

  picker = null;
  setReader(computeReader());
  let frag;
  if (r.name === "who") {
    allProfiles = data;
    frag = renderWho();
  } else frag = r.name === "show" ? renderShow(data) : renderHome(data);
  view.replaceChildren(frag);
  view.dataset.route = r.name;
  updateWhoButton();

  if (keepPlace) {
    view.scrollTop = scrollTop;
    const s = $(".strip", view);
    if (s) s.scrollLeft = stripLeft;
    if (focusKey) {
      const f = $(focusKey, view);
      f?.focus({ preventScroll: true });
      if (keepSearch && f) f.setSelectionRange(keepSearch.start, keepSearch.end);
    }
  }
  updateLive();
}

function setReader(on) {
  reader = on;
  document.body.classList.toggle("is-reader", on);
}

function renderHome(data) {
  const frag = document.createDocumentFragment();
  if (reader) frag.append(searchBox());
  if (data.continue && data.continue.length) {
    const sec = el("section", "row-continue", { "aria-label": tr("Keep watching") });
    const strip = el("div", "strip");
    for (const t of data.continue) strip.append(episodeTile(t, t.kind));
    sec.append(strip);
    frag.append(sec);
  }
  const shows = el("section", "row-shows", { "aria-label": tr("Shows") });
  const grid = el("div", "grid grid-shows");
  for (const s of data.shows || []) grid.append(showTile(s));
  shows.append(grid);
  frag.append(shows);
  if (reader) {
    frag.append(Object.assign(el("p", "no-matches", { role: "status", hidden: "" }), { textContent: tr("No matches") }));
    queueMicrotask(applySearch);
  }
  return frag;
}

// KA-12: filters the shows and the "keep watching" episodes already on the page, by title.
function searchBox() {
  const wrap = el("div", "search");
  const input = el("input", "search-input", {
    type: "search",
    enterkeyhint: "search",
    autocomplete: "off",
    autocapitalize: "off",
    spellcheck: "false",
    placeholder: tr("Search shows and episodes"),
    "aria-label": tr("Search shows and episodes"),
  });
  input.value = searchQuery;
  input.addEventListener("input", () => {
    searchQuery = input.value;
    applySearch();
  });
  wrap.append(input);
  return wrap;
}

function applySearch() {
  if (!reader || route?.name !== "home") return;
  let any = false;
  for (const sec of view.querySelectorAll(".row-continue, .row-shows")) {
    let shown = 0;
    for (const t of sec.querySelectorAll(".tile")) {
      const ok = matchesQuery($(".caption", t)?.textContent, searchQuery);
      t.hidden = !ok;
      if (ok) shown++;
    }
    sec.hidden = shown === 0;
    any ||= shown > 0;
  }
  const none = $(".no-matches", view);
  if (none) none.hidden = any;
}

// PR-2: big round pictures, tap to pick (several allowed), then the big arrow.
function renderWho() {
  const sel = readSelection();
  const keep = sel && selectionValid(sel, allProfiles, state.day) ? sel.profiles : [];
  picker = renderPicker({
    profiles: allProfiles,
    preselected: keep,
    stateProfiles: state.profiles,
    onGo: (ids) => {
      group = ids;
      writeSelection({ profiles: ids, day: state.day, last_used: Date.now() });
      location.hash = "#/";
    },
  });
  return picker.node;
}

function renderShow(data) {
  const frag = document.createDocumentFragment();
  const head = el("div", "show-head");
  const home = el("button", "round-btn btn-home", { type: "button", "aria-label": tr("Home") });
  home.innerHTML = icons.home;
  if (reader) home.append(Object.assign(el("span", "btn-text"), { textContent: tr("Home") }));
  head.append(home);
  const art = mediaBox(data.artwork, showColor(data.show_id), "show-art");
  art.setAttribute("role", "img");
  art.setAttribute("aria-label", data.title);
  head.append(art);
  frag.append(head);

  const sec = el("section", "row-episodes", { "aria-label": data.title });
  const grid = el("div", "grid grid-episodes");
  for (const t of data.episodes || []) grid.append(episodeTile(t));
  sec.append(grid);
  frag.append(sec);
  return frag;
}

// Is there a valid pick on this device? A single kid needs no question. Sets `group`.
function resolveGroup() {
  if (allProfiles.length === 1) {
    group = [allProfiles[0].profile_id];
    return true;
  }
  const sel = readSelection();
  if (selectionValid(sel, allProfiles, state.day)) {
    group = sel.profiles;
    return true;
  }
  group = [];
  return false;
}

// Keep the pick alive while it is in use (the 30-minute idle rule).
function touchSelection() {
  const sel = readSelection();
  if (!sel || Date.now() - lastTouch < 15000) return;
  lastTouch = Date.now();
  writeSelection({ ...sel, last_used: lastTouch });
}

function forgetGroup() {
  clearSelection();
  group = [];
  if (location.hash === "#/who") onRoute();
  else location.hash = "#/who";
}

function updateWhoButton() {
  const show = allProfiles.length > 1 && group.length > 0 && route && route.name !== "who";
  whoBtn.hidden = !show;
  if (show) fillWhoButton(whoBtn, allProfiles, group, reader);
}

function onRoute() {
  const prev = route;
  route = parseRoute();
  if (route.name === "who" && allProfiles.length === 1) {
    history.replaceState(null, "", "#/");
    route = parseRoute();
  }
  if (route.name !== "who" && !resolveGroup()) {
    history.replaceState(null, "", "#/who");
    route = { name: "who" };
  }
  if (route.name !== "who") touchSelection();
  setReader(computeReader());
  updateWhoButton();
  cameFromHome = route.name === "show" && prev?.name === "home";
  view.scrollTop = 0;
  view.replaceChildren();
  loadView();
  if (prev) view.focus({ preventScroll: true });
}

let refreshTimer = null;
function refreshSoon() {
  clearTimeout(refreshTimer);
  if (route?.name === "who") return; // don't reset the taps
  refreshTimer = setTimeout(() => loadView({ keepPlace: true }), 600);
}

// ---------- now-playing bar (KA-6) ----------

const bar = (() => {
  const thumbSlot = el("span", "np-thumb");
  const btn = el("button", "big-btn", { type: "button", "aria-label": tr("Pause") });
  btn.innerHTML = `<span class="big-icon"></span>${icons.spinner}<span class="big-text" hidden></span>`;
  const idle = el("span", "np-idle", { role: "img", "aria-label": tr("Nothing playing") });
  idle.innerHTML = icons.tvSleepy;
  const offline = el("span", "np-offline", { role: "img", "aria-label": tr("TV not reachable") });
  offline.innerHTML = icons.tvOffline;
  // KA-11: the reader UI's text: title, state, time left today and the TV's name.
  const text = el("div", "np-text", { hidden: "" });
  const title = el("span", "np-title");
  const status = el("span", "np-status");
  const left = el("span", "np-left");
  const tv = el("span", "np-tv");
  text.append(title, status, left, tv);
  nowbar.append(idle, offline, thumbSlot, text, btn);
  btn.addEventListener("click", toggle);
  return { thumbSlot, btn, idle, offline, text, title, status, left, tv, thumbSrc: null, icon: null };
})();

function renderBar() {
  const np = state.now_playing;
  const down = tvDown();
  const mode = down ? "offline" : np ? "playing" : "idle";
  nowbar.dataset.mode = mode;
  bar.idle.hidden = mode !== "idle";
  bar.offline.hidden = mode !== "offline";
  bar.thumbSlot.hidden = mode !== "playing";
  bar.btn.hidden = mode !== "playing";
  renderBarText(mode);
  if (mode !== "playing") return;

  if (bar.thumbSrc !== np.thumb) {
    bar.thumbSrc = np.thumb;
    const box = mediaBox(np.thumb, showColor(np.show_id));
    box.setAttribute("role", "img");
    box.setAttribute("aria-label", np.title || "");
    bar.thumbSlot.replaceChildren(box);
  } else {
    bar.thumbSlot.firstChild?.setAttribute("aria-label", np.title || "");
  }
  const paused = np.state === "paused";
  const icon = paused ? "play" : "pause";
  if (bar.icon !== icon) {
    bar.icon = icon;
    $(".big-icon", bar.btn).innerHTML = icons[icon];
  }
  bar.btn.setAttribute("aria-label", paused ? tr("Play") : tr("Pause"));
  const btnText = $(".big-text", bar.btn);
  btnText.hidden = !reader;
  if (reader) btnText.textContent = paused ? tr("Play") : tr("Pause");
  const busy = toggleBusy || np.state === "loading" || np.state === "buffering";
  bar.btn.classList.toggle("is-busy", busy);
  bar.btn.setAttribute("aria-busy", String(busy));
}

// KA-11: text next to the picture controls. Hidden entirely in the icon UI.
function renderBarText(mode) {
  const show = reader;
  bar.text.hidden = !show;
  if (!show) return;
  const np = state.now_playing;
  const eff = effective();
  const status = mode === "offline" ? tr("TV not reachable") : mode === "idle" ? tr("Nothing playing") : statusText(np.state);
  bar.title.textContent = mode === "playing" ? np.title || "" : "";
  bar.title.hidden = mode !== "playing";
  bar.status.textContent = status;
  bar.left.textContent = timeLeftText(eff.sky, eff.time_up);
  bar.tv.textContent = state.device_name ? tr("on %(tv)s", { tv: state.device_name }) : "";
  bar.tv.hidden = !state.device_name;
}

async function toggle() {
  const np = state.now_playing;
  if (toggleBusy || !np || tvDown()) return;
  toggleBusy = true;
  renderBar();
  const r = await (np.state === "paused" ? api.resume() : api.pause());
  toggleBusy = false;
  handleActionResult(r);
  renderBar();
}

// ---------- picks (KA-5, KA-9) ----------

async function pick(tile) {
  if (effective().time_up || playBusy) return;
  if (!resolveGroup()) {
    forgetGroup(); // the day changed or it has been idle: ask who's watching
    return;
  }
  const id = Number(tile.dataset.ep);
  playBusy = true;
  tile.classList.add("is-pending");
  view.classList.add("is-picking");
  touchSelection();
  const r = await api.play(id, group);
  playBusy = false;
  tile.classList.remove("is-pending");
  view.classList.remove("is-picking");
  if (r.status === 404) {
    loadView({ keepPlace: true });
    return;
  }
  if (r.status === 400) {
    forgetGroup();
    return;
  }
  handleActionResult(r);
}

function handleActionResult({ status, data }) {
  if (data && typeof data === "object" && "sky" in data) {
    applyState(data);
    return;
  }
  if (status === 409) {
    // Someone in the group is out of time: draw them at night.
    const profiles = { ...state.profiles };
    for (const id of group) if (profiles[id]) profiles[id] = { ...profiles[id], time_up: true };
    applyState({ ...state, time_up: true, profiles });
  }
  else if (status === 503 || status === 0) applyState({ ...state, tv: "unreachable" });
}

view.addEventListener("click", (e) => {
  const home = e.target.closest(".btn-home");
  if (home) {
    if (cameFromHome) history.back();
    else location.hash = "#/";
    return;
  }
  const tile = e.target.closest(".tile-ep");
  if (tile) {
    pick(tile);
    return;
  }
  const show = e.target.closest(".tile-show");
  if (show && effective().time_up) e.preventDefault();
});

// ---------- live state ----------

// What this device shows: the sky and time-up for its own group only (PR-4). The picker has no
// group yet, so it always shows a day sky.
function effective() {
  if (route?.name === "who") return { ...state, sky: { fraction_left: 1, last_five: false, unlimited: false }, time_up: false };
  return { ...state, ...reduceGroup(state, group) };
}

function updateLive() {
  const eff = effective();
  applySky(eff, skyParts);
  renderBar();
  const current = state.now_playing?.episode_id;
  for (const t of view.querySelectorAll(".tile-ep")) {
    t.classList.toggle("is-current", Number(t.dataset.ep) === current);
  }
  // Night: tiles are dimmed and inert (KA-9). The home button keeps working.
  for (const g of view.querySelectorAll(".strip, .grid")) {
    g.inert = !!eff.time_up;
    g.setAttribute("aria-disabled", String(!!eff.time_up));
  }
  document.body.classList.toggle("tv-down", tvDown());
  picker?.update(state.profiles);
}

function applyState(next) {
  const prev = state;
  const prevEff = effective();
  state = next;
  if (group.some((id) => (next.watching || []).includes(id))) touchSelection(); // watching keeps the pick alive
  if (prev.day && next.day && prev.day !== next.day && route && route.name !== "who") {
    onRoute(); // a new timer day: ask who's watching again
    return;
  }
  updateLive();
  if (route && (prev.now_playing?.episode_id !== next.now_playing?.episode_id || prevEff.time_up !== effective().time_up)) {
    refreshSoon(); // progress and "continue watching" may have changed
  }
}

// ---------- boot ----------

window.addEventListener("hashchange", onRoute);
whoBtn.addEventListener("click", () => {
  location.hash = "#/who";
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && route && route.name !== "who") {
    // The admin may have changed a profile's UI style (KA-11) while the app was in the background.
    api.profiles().then((p) => { allProfiles = p; if (route.name !== "who" && resolveGroup()) loadView({ keepPlace: true }); }, () => {});
    if (resolveGroup()) loadView({ keepPlace: true });
    else onRoute(); // idle for 30 minutes or a new day: ask again
  }
});

updateLive();
subscribe({
  onState: (s) => {
    streamDown = false;
    applyState(s);
  },
  onDown: () => {
    streamDown = true;
    updateLive();
  },
});

// The kids and the server's day come first: they decide whether to ask who's watching.
async function boot() {
  const [p, st] = await Promise.allSettled([api.profiles(), api.state()]);
  if (st.status === "fulfilled") state = { ...state, ...st.value };
  if (p.status !== "fulfilled") {
    setTimeout(boot, 3000);
    return;
  }
  allProfiles = p.value;
  updateLive();
  onRoute();
}
boot();
