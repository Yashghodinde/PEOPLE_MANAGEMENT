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

  // Server error responses surface as toasts instead of failing silently.
  document.body.addEventListener("htmx:responseError", function (e) {
    var msg = "Request failed (" + e.detail.xhr.status + ")";
    try { msg = JSON.parse(e.detail.xhr.responseText).message || msg; } catch (_) {}
    window.dispatchEvent(new CustomEvent("ots-toast", { detail: { kind: "error", message: msg } }));
  });
})();
