// Tellybox receiver: loads a script with retries (CR-6). The Cast SDK comes from gstatic.com, and a
// 1st-gen Chromecast on a flaky network sometimes fails the first request; each attempt is a fresh
// <script> element. Kept apart from cast.js so node can test it (tests/js/sdkload.test.mjs).
// Old-Chrome-safe ES5, like the rest of the receiver.
(function (root) {
  "use strict";

  // load(doc, src, opts, onload, onfail)
  //   opts.delays: the waits before each retry in ms, so delays.length + 1 attempts (default 2 s, 4 s)
  //   opts.wait: setTimeout, replaceable in tests
  //   onload(attempts) after the script loaded; onfail(attempts) when every attempt failed
  function load(doc, src, opts, onload, onfail) {
    var delays = (opts && opts.delays) || [2000, 4000];
    var wait = (opts && opts.wait) || function (fn, ms) { return setTimeout(fn, ms); };
    var attempt = 0;

    function next() {
      attempt++;
      var s = doc.createElement("script");
      s.src = src;
      s.onload = function () { onload(attempt); };
      s.onerror = function () {
        if (s.parentNode) s.parentNode.removeChild(s);
        if (attempt > delays.length) { onfail(attempt); return; }
        wait(next, delays[attempt - 1]);
      };
      doc.head.appendChild(s);
    }
    next();
  }

  var api = { load: load };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.TellyboxSdkLoader = api;
})(typeof window !== "undefined" ? window : this);
