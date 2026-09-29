// Tellybox receiver UI (CR-2..CR-5, CR-8). Pure: no Cast SDK, so dev.html and Node can drive it.
// view(tb, player) says what to show; render(view) puts it on the page.
//   tb     = the last "state" message from the cast service (docs/receiver-protocol.md), or null
//   player = {state: IDLE|LOADING|PLAYING|PAUSED|BUFFERING, currentTime, duration}
// Plain ES5 in an IIFE: the 1st-gen Chromecast's browser is old, and there is no build step.
(function (root) {
  "use strict";

  var UP_NEXT_S = 10; // CR-5: the up-next card shows in the last 10 s of media

  function clamp01(v) { return Math.max(0, Math.min(1, v)); }

  // Copies of sunPosition() and phase() from tellybox/web/static/sky.js: the receiver can't import
  // from /static when it is hosted on GitHub Pages. Keep in step with that file.
  function sunPosition(sky, timeUp) {
    if (timeUp) return { x: 80, y: 140 };
    if (!sky || sky.unlimited || sky.fraction_left == null) return { x: 50, y: 30 };
    var gone = 1 - clamp01(sky.fraction_left);
    return { x: 24 + gone * 54, y: 30 + 56 * Math.pow(gone, 1.35) };
  }

  function phase(sky, timeUp) {
    if (timeUp) return "night";
    if (sky && sky.unlimited) return "unlimited";
    if (sky && sky.last_five) return "dusk";
    return "day";
  }

  function isVideo(s) { return s === "PLAYING" || s === "PAUSED" || s === "BUFFERING"; }

  function view(tb, player) {
    tb = tb || {};
    player = player || {};
    var pstate = player.state || "IDLE";
    var sky = tb.sky || null;
    var timeUp = !!tb.time_up;
    var loading = tb.loading || null;
    var v = { layer: "idle", phase: "day", sun: sunPosition(null, false), cornerSky: false,
              loading: null, upNext: null };

    if ((loading && pstate !== "PLAYING") || pstate === "LOADING") {
      // Row 1. LOADING without a "loading" message (a load we were not told about) also gets the
      // plain loading layer, so the screen doesn't flash the idle sky between two episodes.
      v.layer = "loading";
      v.loading = { artwork: (loading && loading.artwork) || null, thumb: (loading && loading.thumb) || null };
    } else if (isVideo(pstate)) {
      v.layer = "video";
      v.phase = phase(sky, timeUp);
      v.sun = sunPosition(sky, timeUp);
      v.cornerSky = !(sky && sky.unlimited); // hidden when unlimited (CR-2)
      var left = player.duration - player.currentTime;
      if (tb.up_next && tb.up_next.thumb && isFinite(left) && player.duration > 0 && left <= UP_NEXT_S) {
        v.upNext = tb.up_next.thumb;
      }
    } else if (timeUp) {
      v.layer = "night";
      v.phase = "night";
      v.sun = sunPosition(null, true);
    }
    return v;
  }

  // ------------------------------------------------------------------------------ rendering

  var els = null;
  var last = {};
  var onLog = null; // optional (level, msg) sink, set by cast.js

  function byId(id) { return document.getElementById(id); }

  function init() {
    if (els) return;
    var tpl = byId("scene-tpl");
    var corner = byId("corner");
    var scene = byId("scene");
    corner.appendChild(tpl.content.cloneNode(true));
    scene.appendChild(tpl.content.cloneNode(true));
    els = {
      video: byId("video"), corner: corner, scene: scene, loading: byId("loading"),
      artwork: byId("artwork"), thumb: byId("thumb"),
      upnext: byId("upnext"), upnextImg: byId("upnext-img"),
      cornerSun: corner.querySelector(".sun"), sceneSun: scene.querySelector(".sun"),
    };
    // A failed image just leaves the plain layer without it.
    var imgs = [els.artwork, els.thumb, els.upnextImg];
    for (var i = 0; i < imgs.length; i++) {
      imgs[i].onerror = function () {
        this.style.visibility = "hidden";
        if (onLog) onLog("error", "image failed: " + this.getAttribute("src"));
      };
      imgs[i].onload = function () { this.style.visibility = ""; };
    }
  }

  // The sun is drawn at left:0, top:0 and only ever moves by transform (CR-8). translate()
  // percentages are of the sun's own size, so with the sun a fraction f of the container's width
  // and the container's height/width = aspect, a centre at (x%, y%) is:
  function sunTransform(pos, f, aspect) {
    var tx = (pos.x / 100 / f - 0.5) * 100;
    var ty = (pos.y / 100 * aspect / f - 0.5) * 100;
    return "translate(" + tx.toFixed(1) + "%," + ty.toFixed(1) + "%)";
  }

  function set(key, value, apply) {
    if (last[key] === value) return;
    last[key] = value;
    apply(value);
  }

  function setImg(img, url) {
    set(img.id, url || "", function (u) {
      img.style.visibility = "";
      if (u) img.setAttribute("src", u); else img.removeAttribute("src");
    });
  }

  function render(v) {
    init();
    set("scene", v.layer === "night" || v.layer === "idle", function (on) { els.scene.hidden = !on; });
    set("loading", v.layer === "loading", function (on) { els.loading.hidden = !on; });

    var cls = "is-" + v.phase;
    var showCorner = v.layer === "video" && v.cornerSky;
    set("corner", showCorner, function (on) { els.corner.hidden = !on; });
    set("cornerCls", cls, function (c) { els.corner.className = "scene corner " + c; });
    set("sceneCls", cls, function (c) { els.scene.className = "layer scene big " + c; });
    if (showCorner) {
      set("cornerSun", v.sun.x + "," + v.sun.y, function () {
        els.cornerSun.style.transform = sunTransform(v.sun, 0.3, 0.6);
      });
    }
    if (v.layer === "idle" || v.layer === "night") {
      set("sceneSun", v.sun.x + "," + v.sun.y, function () {
        els.sceneSun.style.transform = sunTransform(v.sun, 0.14, 9 / 16);
      });
    }

    var ld = v.loading || {};
    setImg(els.artwork, ld.artwork);
    setImg(els.thumb, ld.thumb);
    set("loadCls", (ld.artwork ? "" : " no-art") + (ld.thumb ? "" : " no-thumb"), function (c) {
      els.loading.className = "layer layer-loading" + c;
    });

    setImg(els.upnextImg, v.upNext);
    set("upnext", !!v.upNext, function (on) { els.upnext.hidden = !on; });
  }

  // ------------------------------------------------------------------------------ state holder

  var tbState = null;
  var playerState = { state: "IDLE", currentTime: 0, duration: 0 };

  function update() { render(view(tbState, playerState)); }
  function setState(tb) { tbState = tb; update(); }
  function setPlayer(p) {
    playerState = { state: p.state, currentTime: p.currentTime || 0, duration: p.duration || 0 };
    update();
  }

  root.TellyboxUI = {
    view: view, render: render, sunPosition: sunPosition, phase: phase,
    setState: setState, setPlayer: setPlayer, update: update,
    setLogger: function (fn) { onLog = fn; },
  };
  if (typeof module !== "undefined" && module.exports) module.exports = root.TellyboxUI;
})(typeof window !== "undefined" ? window : this);
