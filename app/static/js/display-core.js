/* TV board connection rules (M10 section 3). No browser globals are used directly, so the same code runs on the TV
   and in Node tests.
   - Live mode: a message (or the 25 s ping) at least every 60 s. If nothing arrives for 60 s the page reloads.
   - If the live stream fails to connect 3 times in a row, switch to polling the board every 30 s and try the live
     stream again every 5 minutes.
   - "board-changed" fetches the board; "reload" (03:00 each day, or after the display was revoked) reloads the page. */
(function (root) {
  "use strict";
  var QUIET_MS = 60000, POLL_MS = 30000, RETRY_MS = 300000, WATCH_MS = 5000, FAILS = 3;

  function create(o) {
    var errors = 0, mode = "live", lastSeen = o.now(), pollTimer = null, retryTimer = null, watchTimer = null;

    function connect() { o.openStream({ event: onEvent, error: onError }); }
    function stopTimers() {
      if (pollTimer !== null) { o.clearTimeout(pollTimer); pollTimer = null; }
      if (retryTimer !== null) { o.clearTimeout(retryTimer); retryTimer = null; }
    }
    function poll() {
      o.fetchBoard();
      pollTimer = o.setTimeout(poll, POLL_MS);
    }
    function retry() {
      retryTimer = null;
      o.closeStream();
      errors = 0;
      connect(); // if this works, "hello" arrives and polling stops; if not, 3 more errors bring us back here
    }
    function onEvent(type) {
      lastSeen = o.now();
      if (type === "hello") {
        errors = 0;
        if (mode === "poll") { mode = "live"; stopTimers(); }
        o.fetchBoard(); // a fresh connection always starts from the current board
      } else if (type === "board-changed") {
        o.fetchBoard();
      } else if (type === "reload") {
        o.reload();
      }
    }
    function onError() {
      errors += 1;
      if (errors >= FAILS && mode === "live") {
        mode = "poll";
        poll();
        retryTimer = o.setTimeout(retry, RETRY_MS);
      } else if (mode === "poll" && retryTimer === null) {
        retryTimer = o.setTimeout(retry, RETRY_MS);
      }
    }
    function watch() {
      if (mode === "live" && o.now() - lastSeen >= QUIET_MS) { o.reload(); return; }
      watchTimer = o.setTimeout(watch, WATCH_MS);
    }
    return {
      start: function () { lastSeen = o.now(); connect(); watchTimer = o.setTimeout(watch, WATCH_MS); },
      mode: function () { return mode; },
    };
  }

  var api = { create: create, QUIET_MS: QUIET_MS, POLL_MS: POLL_MS, RETRY_MS: RETRY_MS, FAILS: FAILS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.OtsDisplayCore = api;
})(typeof self !== "undefined" ? self : this);
