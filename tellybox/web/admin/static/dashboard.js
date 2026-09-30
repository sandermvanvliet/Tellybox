// Dashboard (AD-3): the TV/timer/connection parts update live from /admin/events (SSE);
// jobs, yt-dlp version and disk usage are polled every 5 s from /admin/api/dashboard (JSON).

import { lang, t, tn } from "./i18n.js";

// Chromecast connection (tellybox.cast.device.ConnectionState), plus our own "unreachable".
const CONNECTION_LABELS = {
  CONNECTING: t("connecting"),
  CONNECTED: t("connected"),
  LOST: t("lost"),
  FAILED: t("failed"),
  DISCONNECTED: t("disconnected"),
  unreachable: t("unreachable"),
};

// Player states (tellybox.cast.device.PlayerState).
const PLAYER_STATE_LABELS = {  // CastController.state(): now_playing.state
  loading: t("loading"),
  playing: t("playing"),
  buffering: t("buffering"),
  paused: t("paused"),
};

// Job statuses and types (tellybox.jobs).
const JOB_STATUS_LABELS = {
  queued: t("queued"),
  downloading: t("downloading"),
  processing: t("processing"),
  ready: t("ready"),
  failed: t("failed"),
};
const JOB_TYPE_LABELS = { download: t("download"), update_ytdlp: t("Update yt-dlp"),
  sb_recheck: t("Check SponsorBlock"), redownload: t("Download again") };

// Titles come from YouTube and profile names from the admin: never put them into markup unescaped.
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function fmtMinutes(seconds) {
  if (seconds == null) return "–";
  const m = Math.round(seconds / 60);
  return m >= 60
    ? t("%(hours)s h %(minutes)s min", { hours: Math.floor(m / 60), minutes: String(m % 60).padStart(2, "0") })
    : t("%(minutes)s min", { minutes: m });
}

function fmtDuration(seconds) {
  if (seconds == null) return "–";
  const s = Math.round(seconds);
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = String(s % 60).padStart(2, "0");
  return hh > 0 ? `${hh}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

function fmtBytes(n) {
  if (n == null) return "–";
  const units = ["B", t("KB"), t("MB"), t("GB"), t("TB")];
  let size = n, i = 0;
  while (size >= 1024 && i < units.length - 1) {
    size /= 1024;
    i++;
  }
  return i === 0 ? t("%(size)s B", { size: Math.round(size) }) : `${size.toFixed(1)} ${units[i]}`;
}

function profilesMeta() {
  const el = document.getElementById("profiles-data");
  if (!el) return [];
  try {
    return JSON.parse(el.textContent);
  } catch {
    return [];
  }
}

function updateConnection(state) {
  const conn = document.getElementById("connection");
  if (conn) {
    const value = state && state.connection ? state.connection : "unreachable";
    conn.textContent = CONNECTION_LABELS[value] ?? value.toLowerCase();
  }
  const device = document.getElementById("device-name");
  if (device) device.textContent = state && state.device ? ` — ${state.device.name}` : "";
}

// "Mila, Noor": the names of the profiles in the current pick, in the admin's order.
function watcherNames(np) {
  const ids = new Set((np && np.profile_ids) || []);
  return profilesMeta().filter((m) => ids.has(m.id)).map((m) => m.name);
}

// The TV receiver line (CR-6); the same three messages as dashboard.html. A fallback that has
// ended is stale state, so it is ignored, and the time is shown in the admin's own locale.
function updateReceiver(state) {
  const row = document.getElementById("receiver-row");
  const el = document.getElementById("receiver");
  if (!row || !el) return;
  const rc = state && state.receiver;
  row.hidden = !rc;
  if (!rc) return;
  const until = rc.fallback_until ? new Date(rc.fallback_until) : null;
  if (until && until > new Date()) {
    const time = until.toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" });
    el.textContent = t("Default Media Receiver (Tellybox receiver unavailable until %(time)s: %(reason)s)",
      { time, reason: rc.last_error || "" });
  } else {
    el.textContent = rc.kind === "tellybox" ? t("Tellybox receiver") : t("Default Media Receiver");
  }
}

function updateNowPlaying(state) {
  const block = document.getElementById("now-playing-content");
  if (!block) return;
  const np = state && state.now_playing;
  if (!np) {
    block.innerHTML = `<p class="muted">${esc(t("Nothing is playing."))}</p>`;
    return;
  }
  const names = watcherNames(np);
  block.innerHTML = `
    <div class="row" style="align-items:flex-start">
      <img class="thumb small" src="/admin/img/episode/${esc(np.episode_id)}.jpg" alt="">
      <div>
        <h2>${esc(np.title)}</h2>
        <p><span class="badge">${esc(PLAYER_STATE_LABELS[np.state] ?? np.state)}</span> ${fmtDuration(np.position_s)} / ${fmtDuration(np.duration_s)}</p>
        <p class="muted watchers">${names.length ? esc(t("Watching: %(names)s", { names: names.join(", ") })) : ""}</p>
      </div>
    </div>
    <form method="post" action="/admin/stop" class="inline"><button class="btn danger">${esc(t("Stop"))}</button></form>`;
}

function updateProfiles(state) {
  const timers = {};
  for (const p of (state && state.timer && state.timer.profiles) || []) timers[p.profile_id] = p;
  const watching = new Set((state && state.now_playing && state.now_playing.profile_ids) || []);
  for (const m of profilesMeta()) {
    const row = document.getElementById(`profile-${m.id}`);
    if (!row) continue;
    const tm = timers[m.id];
    const usedS = tm ? tm.used_s : 0;
    const extraS = tm ? tm.extra_s : 0;
    const unlimited = tm ? !!tm.unlimited : false;
    const blocked = tm ? !!tm.blocked : false;
    const remaining = unlimited ? null : Math.max(0, m.allowance_min * 60 + extraS - usedS);

    const usedEl = row.querySelector(".used");
    if (usedEl) {
      // The same two messages as dashboard.html, joined the same way.
      usedEl.textContent = t("%(time)s used", { time: fmtMinutes(usedS) }) +
        (remaining !== null ? ` · ${t("%(time)s left", { time: fmtMinutes(remaining) })}` : "");
    }
    const unlimitedBadge = row.querySelector(".unlimited-badge");
    if (unlimitedBadge) unlimitedBadge.classList.toggle("hidden", !unlimited);
    const blockedBadge = row.querySelector(".blocked-badge");
    if (blockedBadge) blockedBadge.classList.toggle("hidden", !blocked);
    const watchingBadge = row.querySelector(".watching-badge");
    if (watchingBadge) watchingBadge.classList.toggle("hidden", !watching.has(m.id));
  }
}

function connectEvents() {
  const source = new EventSource("/admin/events");
  source.onmessage = (ev) => {
    let state = null;
    try {
      state = JSON.parse(ev.data);
    } catch {
      return;
    }
    updateConnection(state);
    updateReceiver(state);
    updateNowPlaying(state);
    updateProfiles(state);
  };
}

async function refreshDashboardApi() {
  try {
    const res = await fetch("/admin/api/dashboard");
    if (!res.ok) return;
    const data = await res.json();

    const version = document.getElementById("ytdlp-version");
    if (version) version.textContent = data.ytdlp_version || t("unknown");

    const disk = document.getElementById("disk-usage");
    if (disk) {
      const media = fmtBytes(data.disk.media_bytes);
      disk.textContent = t("Media on disk: %(size)s", { size: media }) +
        (data.disk.free_bytes != null ? ` · ${t("Free space: %(size)s", { size: fmtBytes(data.disk.free_bytes) })}` : "");
    }

    const failedEl = document.getElementById("jobs-failed");
    if (failedEl) {
      const n = data.jobs.failed.length;
      failedEl.textContent = tn("%(num)d failed job", "%(num)d failed jobs", n);
      failedEl.classList.toggle("hidden", n === 0);
    }

    const activeEl = document.getElementById("jobs-active");
    if (activeEl) {
      activeEl.innerHTML = data.jobs.active.length
        ? data.jobs.active.map((j) => `<li>${esc(JOB_TYPE_LABELS[j.type] ?? j.type)} #${esc(j.target_id)} — ` +
            `${esc(JOB_STATUS_LABELS[j.status] ?? j.status)}` +
            `${j.progress != null ? ` (${Math.round(j.progress * 100)}%)` : ""}</li>`).join("")
        : `<li class="muted">${esc(t("Nothing downloading."))}</li>`;
    }
  } catch {
    // The next tick will try again; keep showing the last known values.
  }
}

connectEvents();
refreshDashboardApi();
setInterval(refreshDashboardApi, 5000);
