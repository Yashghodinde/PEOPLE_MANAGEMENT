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
  // M08: the server tells the page a file reached SharePoint, so "Not yet off-site" clears at once. (The files list
  // also refreshes itself every 15 s while anything is pending, so nothing depends on this arriving.)
  es.addEventListener("files-changed", function () { document.body.dispatchEvent(new CustomEvent("ots:files-changed")); });
  window.addEventListener("beforeunload", function () { es.close(); });
})();
