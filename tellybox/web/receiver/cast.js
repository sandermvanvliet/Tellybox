// Tellybox receiver: the thin Cast Application Framework (CAF) glue. All SDK calls live here, so
// this file can be swapped for the legacy receiver SDK without touching ui.js. See
// docs/receiver-protocol.md. With ?dev in the URL the SDK isn't loaded (dev.html drives ui.js).
(function () {
  "use strict";

  var NS = "urn:x-cast:tellybox";
  var SDK = "//www.gstatic.com/cast/sdk/libs/caf_receiver/v3/cast_receiver_framework.js";
  var STATS_EVERY_MS = 60000;

  var UI = window.TellyboxUI;
  UI.update(); // first paint: the idle sky, until a state arrives

  if (/[?&]dev\b/.test(location.search)) return;

  var ctx = null;
  var pm = null;
  var video = document.getElementById("video");
  var player = { state: "IDLE", currentTime: 0, duration: 0 };

  function send(obj) {
    try { ctx.sendCustomMessage(NS, undefined, obj); } catch (e) { /* not connected yet */ }
  }
  function log(level, msg) { if (ctx) send({ type: "log", v: 1, level: level, msg: String(msg).slice(0, 300) }); }
  UI.setLogger(log);
  window.addEventListener("error", function (e) { log("error", "JS error: " + e.message); });

  function frames() {
    if (video.getVideoPlaybackQuality) {
      var q = video.getVideoPlaybackQuality();
      return { dropped: q.droppedVideoFrames, total: q.totalVideoFrames };
    }
    if ("webkitDroppedFrameCount" in video) {
      return { dropped: video.webkitDroppedFrameCount, total: video.webkitDecodedFrameCount };
    }
    return null;
  }

  function push() {
    UI.setPlayer(player);
  }

  // Player state from the framework (PlayerState: IDLE, PLAYING, PAUSED, BUFFERING).
  function syncState(loadingHint) {
    var s = loadingHint ? "LOADING" : String(pm.getPlayerState() || "IDLE");
    if (s !== "IDLE" && s !== "PLAYING" && s !== "PAUSED" && s !== "BUFFERING") s = "IDLE";
    // BUFFERING before the first frame of a new load is still loading; the "loading" message
    // decides that (ui.js), so pass the state on as it is.
    if (s === player.state && !loadingHint) return;
    if (loadingHint) { player.currentTime = 0; player.duration = 0; }
    player.state = s;
    push();
  }

  function timeTick() {
    player.currentTime = video.currentTime || 0;
    player.duration = video.duration || 0;
    push();
  }

  function start() {
    if (!window.cast || !cast.framework) { return; }
    var E = cast.framework.events.EventType;
    ctx = cast.framework.CastReceiverContext.getInstance();
    pm = ctx.getPlayerManager();
    pm.setMediaElement(video); // our own <video>: no CAF player UI (docs/spike-receiver.md, S2)

    ctx.addCustomMessageListener(NS, function (e) {
      var d = e.data;
      if (d && d.type === "state") UI.setState(d);
    });
    ctx.addEventListener(cast.framework.system.EventType.SENDER_CONNECTED, function () {
      send({ type: "hello", v: 1, ua: navigator.userAgent });
    });

    pm.addEventListener(E.LOAD_START, function () { syncState(true); });
    var others = [E.BUFFERING, E.PLAYING, E.PAUSE, E.MEDIA_FINISHED, E.ERROR];
    for (var i = 0; i < others.length; i++) {
      pm.addEventListener(others[i], function () { syncState(false); });
    }
    pm.addEventListener(E.ERROR, function (e) { log("error", "player error " + e.detailedErrorCode + " " + (e.reason || "")); });
    pm.addEventListener(E.TIME_UPDATE, timeTick);
    // The video element as a second source, in case a framework event is missed.
    video.addEventListener("ended", function () { syncState(false); });
    video.addEventListener("emptied", function () { syncState(false); });

    // CR-8: dropped frames, so real-device checks can compare with and without the overlay.
    setInterval(function () {
      var f = frames();
      if (f && player.state !== "IDLE") send({ type: "stats", v: 1, dropped: f.dropped, total: f.total, state: player.state });
    }, STATS_EVERY_MS);

    var opts = new cast.framework.CastReceiverOptions();
    opts.disableIdleTimeout = true; // the cast service decides when to quit (night hold)
    opts.customNamespaces = {};
    opts.customNamespaces[NS] = cast.framework.system.MessageType.JSON;
    ctx.start(opts);
  }

  var s = document.createElement("script");
  s.src = SDK;
  s.onload = start;
  s.onerror = function () { /* no SDK (offline): the idle sky stays; the cast service falls back (CR-6) */ };
  document.head.appendChild(s);
})();
