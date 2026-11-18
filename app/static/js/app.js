/* Global HTMX configuration. CSP-safe: no eval, no inline styles injected by htmx. */
(function () {
  "use strict";
  if (!window.htmx) return;
  htmx.config.defaultSwapStyle = "outerHTML";
  htmx.config.allowEval = false;
  htmx.config.allowScriptTags = false;
  htmx.config.includeIndicatorStyles = false;

  // RFC 9562 UUIDv7: 48-bit unix ms timestamp + random.
  function uuid7() {
    var ms = Date.now(), b = new Uint8Array(16);
    crypto.getRandomValues(b);
    for (var i = 5; i >= 0; i--) { b[i] = ms & 0xff; ms = Math.floor(ms / 256); }
    b[6] = (b[6] & 0x0f) | 0x70; b[8] = (b[8] & 0x3f) | 0x80;
    var h = Array.prototype.map.call(b, function (x) { return ("0" + x.toString(16)).slice(-2); }).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }
  window.otsUuid7 = uuid7;

  // Every data-changing HTMX request carries an Idempotency-Key (the M09 outbox replaces this for offline use).
  document.body.addEventListener("htmx:configRequest", function (e) {
    var verb = (e.detail.verb || "").toLowerCase();
    if (verb !== "get" && !e.detail.headers["Idempotency-Key"]) e.detail.headers["Idempotency-Key"] = uuid7();
  });

  // Validation errors (422) carry the corrected form: swap them in place instead of dropping them.
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    if (e.detail.xhr.status === 422) { e.detail.shouldSwap = true; e.detail.isError = false; }
  });

  // Server error responses surface as toasts instead of failing silently.
  document.body.addEventListener("htmx:responseError", function (e) {
    var msg = "Request failed (" + e.detail.xhr.status + ")";
    try { msg = JSON.parse(e.detail.xhr.responseText).message || msg; } catch (_) {}
    window.dispatchEvent(new CustomEvent("ots-toast", { detail: { kind: "error", message: msg } }));
  });

  // Back: the browser's own history when the person came from inside the app (keeps scroll and filters), else the parent page.
  document.addEventListener("click", function (e) {
    var a = e.target.closest("[data-back]");
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
    var ref = document.referrer, same = false;
    try { same = !!ref && new URL(ref).origin === location.origin; } catch (_) {}
    if (same && history.length > 1) { e.preventDefault(); history.back(); }
  });

  // Persistent shell (IMP-15.2): the sidebar and the top bar stay put; only the content area, the page title, the sidebar
  // highlight and the bell are replaced. Anything that is not an ordinary page (a download, a preview, logout, an
  // error, a login redirect, another site) is left to the browser, so nothing can be swapped into the page by mistake.
  var PLAIN = /\/(download|preview|logout|login|static|media|healthz|live|display)(\/|$)|\.(pdf|png|jpe?g|zip|xlsx?|docx?|txt|csv)$/i;
  var shellBusy = null;

  function plain(url) { window.location.assign(url); }

  function go(url, push) {
    if (shellBusy) shellBusy.abort();
    var ctl = shellBusy = new AbortController();
    fetch(url, { credentials: "same-origin", signal: ctl.signal, headers: { "X-OTS-Shell": "1", Accept: "text/html" } }).then(function (r) {
      var type = r.headers.get("content-type") || "";
      var landed = new URL(r.url, location.href);
      if (!r.ok || type.indexOf("text/html") === -1 || PLAIN.test(landed.pathname)) return plain(url);
      return r.text().then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var main = doc.getElementById("main"), mine = document.getElementById("main");
        if (!main || !mine || !doc.getElementById("shell-title")) return plain(url);
        if (push) history.pushState({ shell: 1 }, "", landed.pathname + landed.search);
        mine.innerHTML = main.innerHTML;
        ["shell-title", "sidebar-nav", "shell-tools"].forEach(function (id) {
          var a = document.getElementById(id), b = doc.getElementById(id);
          if (a && b) a.innerHTML = b.innerHTML;
        });
        document.title = doc.title;
        if (window.htmx) window.htmx.process(mine);
        window.scrollTo(0, 0);
        mine.setAttribute("tabindex", "-1"); mine.focus({ preventScroll: true });
        document.dispatchEvent(new CustomEvent("ots:page", { detail: { url: url } }));
      });
    }).catch(function (err) { if (!err || err.name !== "AbortError") plain(url); });
  }

  document.addEventListener("click", function (e) {
    var a = e.target.closest ? e.target.closest("a[href]") : null;
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (a.target || a.hasAttribute("download") || a.hasAttribute("data-no-boost") || a.closest('[hx-boost="false"]')) return;
    var u;
    try { u = new URL(a.href, location.href); } catch (_) { return; }
    if (u.origin !== location.origin || PLAIN.test(u.pathname) || !document.getElementById("shell-title")) return;
    if (u.pathname === location.pathname && u.search === location.search) { if (u.hash) return; }
    e.preventDefault();
    go(u.pathname + u.search, true);
  });

  // a search box (a GET form) navigates the same way
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (e.defaultPrevented || !f || (f.method || "get").toLowerCase() !== "get" || f.hasAttribute("data-no-boost")) return;
    var action = new URL(f.getAttribute("action") || location.pathname, location.href);
    if (action.origin !== location.origin || PLAIN.test(action.pathname) || !document.getElementById("shell-title")) return;
    e.preventDefault();
    var q = new URLSearchParams(new FormData(f)).toString();
    go(action.pathname + (q ? "?" + q : ""), true);
  });

  window.addEventListener("popstate", function () { go(location.pathname + location.search, false); });

  // A picture that cannot load (missing file, 403, 404, expired) is replaced by the person's initials, never a broken
  // image icon (IMP-16). `error` does not bubble, so it is caught on the way down.
  function initialsFor(name) {
    return (name || "").trim().split(/\s+/).slice(0, 2).map(function (p) { return p.charAt(0).toUpperCase(); }).join("");
  }
  function fallBack(img) {
    if (!img || img.dataset.fellBack) return;
    img.dataset.fellBack = "1";
    var big = img.dataset.size === "5", span = document.createElement("span");
    span.className = "inline-flex items-center justify-center rounded-full bg-line text-ink-900 font-semibold " + (big ? "w-6 h-6 text-sec" : "w-5 h-5 text-cap");
    span.title = img.dataset.name || ""; span.setAttribute("aria-label", img.dataset.name || "");
    span.textContent = initialsFor(img.dataset.name);
    if (img.parentNode) img.parentNode.replaceChild(span, img);
  }
  document.addEventListener("error", function (e) { if (e.target && e.target.tagName === "IMG" && e.target.hasAttribute("data-avatar")) fallBack(e.target); }, true);
  function sweepAvatars() {
    Array.prototype.forEach.call(document.querySelectorAll("img[data-avatar]"), function (img) { if (img.complete && img.naturalWidth === 0) fallBack(img); });
  }
  document.addEventListener("DOMContentLoaded", sweepAvatars);
  document.addEventListener("ots:page", sweepAvatars);  // after the shell swaps a page in
})();
