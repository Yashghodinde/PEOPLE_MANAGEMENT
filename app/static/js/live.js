/* Live connection (M10.1). One EventSource per page; the browser reconnects by itself after `retry: 5000`.
   Messages carry ids only. Here only "logged-out" is handled; page refreshes arrive with M10.3. */
(function () {
  "use strict";
  if (!window.EventSource) return;
  var es = new EventSource("/live/stream");
  es.addEventListener("logged-out", function (e) {
    var reason = "";
    try { reason = JSON.parse(e.data).reason || ""; } catch (x) {}
    es.close();
    window.location.href = "/login?reason=" + encodeURIComponent(reason || "ended");
  });
  window.addEventListener("beforeunload", function () { es.close(); });
})();
