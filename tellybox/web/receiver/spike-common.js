// Step 13 spike (docs/plans/step13-receiver.md, S1..S9). Shared by spike.html (CAF) and
// spike-v2.html (legacy receiver SDK). Temporary: removed once the spike is done.
// Shows a big on-screen log so results can be read off the TV even if the SDK fails.
"use strict";
const NS = "urn:x-cast:tellybox";
const T0 = performance.now();
const logEl = document.getElementById("log");
const lines = [];
let transport = null; // {send(obj)} once the SDK is up

function log(msg, level = "info") {
  const line = `${((performance.now() - T0) / 1000).toFixed(1)}s ${msg}`;
  lines.push(line);
  while (lines.length > 9) lines.shift();
  logEl.textContent = lines.join("\n");
  try { transport && transport.send({ type: "log", v: 1, level, msg: line }); } catch (e) { /* ignore */ }
}
window.addEventListener("error", (e) => log(`JS error: ${e.message}`, "error"));
log(`page loaded; ${location.protocol}; UA ${navigator.userAgent}`);

// Corner sky with a sun that moves every 5 s while the overlay is on (S6: worst case for CR-8).
const sky = document.getElementById("sky");
const sun = document.getElementById("sun");
let overlayTimer = null;
function setOverlay(on) {
  sky.hidden = !on;
  clearInterval(overlayTimer);
  if (on) {
    let t = 0;
    overlayTimer = setInterval(() => {
      t = (t + 1) % 20;
      sun.style.transform = `translate(${10 + t * 5}px, ${8 + t * 2.5}px)`;
    }, 5000);
  }
  log(`overlay ${on ? "on" : "off"}`);
}

// S3: an image from the LAN over HTTP, from this HTTPS page.
const img = document.getElementById("img");
function showImage(url) {
  img.onload = () => log(`image ok ${img.naturalWidth}x${img.naturalHeight}`);
  img.onerror = () => log(`image FAILED ${url}`, "error");
  img.src = url;
}

function frames(video) {
  if (!video) return null;
  if (video.getVideoPlaybackQuality) {
    const q = video.getVideoPlaybackQuality();
    return { dropped: q.droppedVideoFrames, total: q.totalVideoFrames };
  }
  if ("webkitDroppedFrameCount" in video) {
    return { dropped: video.webkitDroppedFrameCount, total: video.webkitDecodedFrameCount };
  }
  return null;
}

function watchVideo(video) {
  for (const ev of ["loadstart", "loadedmetadata", "playing", "pause", "waiting", "stalled", "ended", "error"]) {
    video.addEventListener(ev, () => {
      const err = ev === "error" && video.error ? ` code=${video.error.code}` : "";
      log(`video ${ev}${err} t=${video.currentTime.toFixed(1)}`, ev === "error" ? "error" : "info");
    });
  }
  setInterval(() => {
    const f = frames(video);
    if (f && transport) transport.send({ type: "stats", v: 1, ...f, state: video.paused ? "PAUSED" : "PLAYING", t: video.currentTime });
  }, 10000);
}

function onMessage(data) {
  if (!data || typeof data !== "object") return;
  if (data.type === "overlay") setOverlay(!!data.on);
  else if (data.type === "image") showImage(data.url);
  else if (data.type === "logview") logEl.hidden = !data.on;
  else if (data.type === "ping") transport.send({ type: "pong", v: 1, echo: data.echo ?? null });
  else log(`message ${JSON.stringify(data).slice(0, 80)}`);
}

function hello() {
  transport.send({ type: "hello", v: 1, ua: navigator.userAgent, since_load_s: (performance.now() - T0) / 1000 });
}
