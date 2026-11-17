/* The office TV page (M10 section 3). Vanilla JavaScript, well under 30 KB in total with display-core.js. */
(function () {
  "use strict";
  var Core = window.OtsDisplayCore, board = document.getElementById("board");
  if (!Core || !board) return;
  var es = null, offset = 0; // offset: the server's clock minus this TV's clock, so the clock shown is the server's

  function two(n) { return n < 10 ? "0" + n : "" + n; }
  function tick() {
    var el = document.getElementById("clock");
    if (!el) return;
    // The office clock runs in India Standard Time (UTC+5:30), whatever the TV is set to.
    var t = new Date(Date.now() + offset + 330 * 60000);
    el.textContent = two(t.getUTCHours()) + ":" + two(t.getUTCMinutes());
  }
  function fetchBoard() {
    fetch("/display/board", { credentials: "same-origin", cache: "no-store" }).then(function (r) {
      if (r.status === 401) { window.location.href = "/display/pair"; return null; }
      return r.ok ? r.text() : null;
    }).then(function (html) { if (html) { board.innerHTML = html; tick(); } }).catch(function () {});
  }
  var core = Core.create({
    now: Date.now,
    setTimeout: function (f, ms) { return window.setTimeout(f, ms); },
    clearTimeout: function (id) { window.clearTimeout(id); },
    fetchBoard: fetchBoard,
    reload: function () { window.location.reload(); },
    closeStream: function () { if (es) { es.close(); es = null; } },
    openStream: function (h) {
      es = new EventSource("/live/stream");
      ["hello", "ping", "board-changed", "reload"].forEach(function (type) {
        es.addEventListener(type, function (e) {
          if (type === "hello") { try { offset = Date.parse(JSON.parse(e.data).server_time) - Date.now(); } catch (x) {} }
          h.event(type);
        });
      });
      es.onerror = function () { h.error(); };
    },
  });
  core.start();
  window.setInterval(tick, 1000);
})();
