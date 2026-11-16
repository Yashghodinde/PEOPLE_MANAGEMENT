/* Live connection (M10.1, M10.3). One EventSource per page; the browser reconnects by itself after `retry: 5000`.
   Messages carry ids only. A message makes the page re-fetch what it shows, so nobody has to press reload:
   - the task page, task list and dashboard swap their content when the task changed (never while someone is typing,
     has a dialog open or an upload running; text typed into a field is kept across the swap);
   - the bell count refreshes on a new notification;
   - the files list refreshes when a file reaches SharePoint;
   - on reconnect everything visible is fetched again, so a message missed while offline cannot leave a stale page. */
(function () {
  "use strict";
  if (!window.EventSource || !window.OtsLiveCore) return;
  var Core = window.OtsLiveCore;
  var es = new EventSource("/live/stream");
  var opened = false;

  function toast(kind, text) { if (window.otsToast) window.otsToast(kind, text); }

  // ---- is it safe to swap the page right now? ----
  function isSafe() {
    if (document.hidden) return false; // refreshed when the tab is shown again
    var main = document.getElementById("main");
    if (!main) return false;
    var a = document.activeElement;
    if (a && main.contains(a)) {
      var tag = a.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || a.isContentEditable) return false;
    }
    if (document.body.hasAttribute("data-live-hold")) return false; // an upload is running
    if (document.querySelector("dialog[open], [aria-modal='true']")) return false;
    return true;
  }

  // ---- typed text survives a swap ----
  document.addEventListener("input", function (e) {
    var t = e.target;
    if (t && t.dataset && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) t.dataset.liveDirty = "1";
  }, true);
  function keepDrafts(main) {
    var kept = [];
    main.querySelectorAll("[data-live-dirty]").forEach(function (el) {
      var form = el.form;
      if (!form || !el.name || el.type === "password" || el.type === "file") return;
      kept.push({ action: form.getAttribute("action") || "", name: el.name, value: el.value, checked: el.checked, type: el.type });
    });
    return kept;
  }
  function restoreDrafts(main, kept) {
    kept.forEach(function (k) {
      main.querySelectorAll("form").forEach(function (form) {
        if ((form.getAttribute("action") || "") !== k.action) return;
        var el = form.elements[k.name];
        if (!el || el.length) return; // a group of radios or checkboxes is not restored
        if (k.type === "checkbox" || k.type === "radio") el.checked = k.checked; else el.value = k.value;
        el.dataset.liveDirty = "1";
      });
    });
  }

  // ---- the three re-fetches ----
  function get(url) {
    return fetch(url, { credentials: "same-origin", headers: { "X-Requested-With": "ots-live" } }).then(function (r) {
      if (r.status === 401 || r.redirected && /\/login/.test(r.url)) { window.location.href = "/login?reason=ended"; throw new Error("signed out"); }
      if (!r.ok) throw new Error("status " + r.status);
      return r.text();
    });
  }
  function refreshPage() {
    var url = window.location.pathname + window.location.search;
    return get(url).then(function (html) {
      var doc = new DOMParser().parseFromString(html, "text/html");
      var fresh = doc.getElementById("main");
      var main = document.getElementById("main");
      if (!fresh || !main || !isSafe()) return; // things changed while we fetched: try again on the next message
      if (window.location.pathname + window.location.search !== url) return;
      var kept = keepDrafts(main);
      main.innerHTML = fresh.innerHTML;
      restoreDrafts(main, kept);
      if (window.htmx) window.htmx.process(main);
      var h = doc.querySelector("header h1"), mine = document.querySelector("header h1");
      if (h && mine) mine.textContent = h.textContent;
    });
  }
  function refreshBell() {
    return get("/app/notifications/badge").then(function (html) {
      var cur = document.getElementById("ots-bell");
      if (!cur) return;
      var t = document.createElement("template");
      t.innerHTML = html.trim();
      if (t.content.firstElementChild) cur.replaceWith(t.content.firstElementChild);
    });
  }

  var page = Core.scheduler({
    refresh: refreshPage, isSafe: isSafe, now: Date.now,
    setTimeout: function (f, ms) { return window.setTimeout(f, ms); },
    onStale: function () { toast("warning", "This page has new changes. They will appear as soon as you finish typing."); },
  });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) page.request(); });

  function act(w) {
    if (w.page) page.request();
    if (w.bell || w.page) refreshBell().catch(function () {}); // opening a task also marks its notifications read
    if (w.files) document.body.dispatchEvent(new CustomEvent("ots:files-changed"));
  }
  ["task-changed", "notification", "file-archived", "reload"].forEach(function (type) {
    es.addEventListener(type, function (e) {
      var msg = {};
      try { msg = JSON.parse(e.data); } catch (x) {}
      msg.type = type;
      act(Core.wants(msg, window.location.pathname));
    });
  });
  es.addEventListener("alert", function (e) {
    var msg = {};
    try { msg = JSON.parse(e.data); } catch (x) {}
    toast(msg.severity === "CRITICAL" ? "error" : "warning", "A system alert was raised. Open System health to see it.");
  });
  es.addEventListener("logged-out", function (e) {
    var reason = "";
    try { reason = JSON.parse(e.data).reason || ""; } catch (x) {}
    es.close();
    window.location.href = "/login?reason=" + encodeURIComponent(reason || "ended");
  });
  // First connection: nothing to catch up on. Every later one: fetch what was missed while the line was down.
  es.addEventListener("hello", function () {
    if (opened) act({ page: Core.pageKind(window.location.pathname).kind !== "other", bell: true, files: true });
    opened = true;
  });
  window.addEventListener("beforeunload", function () { es.close(); });
})();
