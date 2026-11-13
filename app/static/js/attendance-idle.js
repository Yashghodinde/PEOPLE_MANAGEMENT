/* Idle reporting (M06.3). Two modes:
   OS       - the browser's IdleDetector watches the whole computer (needs a one-time permission);
   TAB_ONLY - fallback: only activity inside this tab counts, idle after 45 minutes without any.
   Only the state is sent; the server uses its own clock. Every post retries 3 times with backoff. */
(function () {
  "use strict";
  var TAB_LIMIT_MS = 45 * 60 * 1000, THRESHOLD_MS = 60000;
  function csrf() {
    try { return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRFToken"] || ""; } catch (e) { return ""; }
  }
  function post(url, fields, attempt) {
    attempt = attempt || 0;
    var body = new URLSearchParams(fields);
    return fetch(url, { method: "POST", body: body, credentials: "same-origin", headers: { "X-CSRFToken": csrf() } })
      .then(function (r) { if (r.status >= 500 && attempt < 3) throw new Error("retry"); return r; })
      .catch(function () {
        if (attempt >= 3) return null;
        return new Promise(function (ok) { setTimeout(ok, 500 * Math.pow(2, attempt)); })
          .then(function () { return post(url, fields, attempt + 1); });
      });
  }
  var current = "active";
  function report(state) {
    if (state === current) return;
    current = state;
    post("/attendance/idle", { state: state });
  }
  function setMode(mode) { return post("/attendance/mode", { idle_mode: mode }); }

  function tabOnly() {
    setMode("TAB_ONLY");
    var timer = null;
    function arm() { clearTimeout(timer); timer = setTimeout(function () { report("idle"); }, TAB_LIMIT_MS); }
    function activity() { if (current === "idle") report("active"); arm(); }
    ["mousemove", "keydown", "mousedown", "touchstart", "scroll"].forEach(function (n) {
      window.addEventListener(n, activity, { passive: true });
    });
    document.addEventListener("visibilitychange", function () { if (!document.hidden) activity(); });
    arm();
  }

  function osMode() {
    var detector = new IdleDetector();
    detector.addEventListener("change", function () {
      var idle = detector.userState === "idle" || detector.screenState === "locked";
      report(idle ? "idle" : "active");
    });
    return detector.start({ threshold: THRESHOLD_MS }).then(function () { return setMode("OS"); });
  }

  function banner(onClick) {
    var bar = document.createElement("div");
    bar.className = "bg-canvas border-b border-line px-5 py-2 flex items-center gap-3 text-body";
    bar.innerHTML = '<span>Attendance tracking needs one permission on this computer.</span>';
    var b = document.createElement("button");
    b.type = "button"; b.textContent = "Enable attendance tracking";
    b.className = "h-5 px-4 rounded-full bg-accent text-canvas text-body font-medium";
    b.addEventListener("click", function () { onClick().then(function () { bar.remove(); }); });
    bar.appendChild(b);
    document.body.insertBefore(bar, document.body.firstChild);
  }

  function start() {
    if (!("IdleDetector" in window)) { tabOnly(); return; }
    var ask = navigator.permissions && navigator.permissions.query
      ? navigator.permissions.query({ name: "idle-detection" }) : Promise.resolve({ state: "prompt" });
    ask.then(function (p) {
      if (p.state === "granted") { osMode().catch(tabOnly); return; }
      tabOnly(); // the safe mode until permission is given
      if (p.state === "prompt") {
        banner(function () {
          return IdleDetector.requestPermission().then(function (r) { if (r === "granted") return osMode(); });
        });
      }
    }).catch(tabOnly);
  }
  window.otsAttendanceIdle = { start: start, report: report };
  if (document.body && document.body.getAttribute("data-attendance") === "1") start();
})();
