/* Live page refresh rules (M10.3). No browser globals are used directly, so the same code runs in the page and in
   Node tests. Messages carry ids only; this decides which part of the page to re-fetch, and when it is safe. */
(function (root) {
  "use strict";
  var UUID = "[0-9a-fA-F-]{36}";
  var DETAIL = new RegExp("^/app/tasks/(" + UUID + ")/?$");

  // What kind of page is this? Only these are ever refreshed under the person's hands; forms never are.
  function pageKind(pathname) {
    var m = DETAIL.exec(pathname);
    if (m) return { kind: "detail", id: m[1].toLowerCase() };
    if (pathname === "/app/tasks" || pathname === "/app/tasks/") return { kind: "landing" };
    if (pathname === "/app/tasks/all" || pathname === "/app/tasks/all/") return { kind: "list" };
    if (/^\/app\/tasks\/(by|to)\/[^/]+\/?$/.test(pathname)) return { kind: "group" };
    if (pathname === "/app/dashboard" || pathname === "/app/dashboard/") return { kind: "dashboard" };
    if (pathname === "/app/notifications" || pathname === "/app/notifications/") return { kind: "notifications" };
    return { kind: "other" };
  }

  // message + path -> what to re-fetch: {page, bell, files, alert}
  function wants(msg, pathname) {
    var out = { page: false, bell: false, files: false, alert: false };
    var p = pageKind(pathname);
    switch (msg && msg.type) {
      case "task-changed":
        if (p.kind === "detail") out.page = String(msg.task_id || "").toLowerCase() === p.id;
        else out.page = p.kind === "list" || p.kind === "dashboard" || p.kind === "landing" || p.kind === "group";
        break;
      case "notification":
        out.bell = true;
        // a new message changes the unread marks on the cards, so the card pages refresh as well
        out.page = p.kind === "notifications" || p.kind === "landing" || p.kind === "group";
        break;
      case "file-archived":
        out.files = true;
        break;
      case "alert":
        out.alert = true;
        break;
      case "reload":
        out.bell = out.files = true;
        out.page = p.kind !== "other";
        break;
    }
    return out;
  }

  // Waits a moment so a burst of messages is one fetch, and holds while the person is busy.
  function scheduler(o) {
    var DEBOUNCE = o.debounce == null ? 400 : o.debounce;
    var RECHECK = o.recheck == null ? 1500 : o.recheck;
    var STALE_AFTER = o.staleAfter == null ? 12000 : o.staleAfter;
    var timer = null, running = false, again = false, since = null, told = false;

    function fire() {
      timer = null;
      if (running) { again = true; return; }
      if (!o.isSafe()) {
        if (since === null) since = o.now();
        if (!told && o.now() - since >= STALE_AFTER) { told = true; if (o.onStale) o.onStale(); }
        timer = o.setTimeout(fire, RECHECK);
        return;
      }
      since = null; told = false; running = true;
      Promise.resolve().then(o.refresh).catch(function () {}).then(function () {
        running = false;
        if (again) { again = false; request(); }
      });
    }
    function request() {
      if (timer !== null) return;
      timer = o.setTimeout(fire, DEBOUNCE);
    }
    return { request: request, pending: function () { return timer !== null || running; } };
  }

  // Anything that must not be swapped out from under the person (an upload, a recording, a voice note playing)
  // takes a hold and gives it back. Several can be held at once; when the last one is released the page that
  // was waiting for a refresh gets it (onRelease).
  function holdCounter() {
    var held = {}, n = 0, seq = 0, api = { onRelease: null };
    api.acquire = function (reason) { var id = ++seq; held[id] = reason || "busy"; n++; return id; };
    api.release = function (id) {
      if (!(id in held)) return;
      delete held[id]; n--;
      if (n === 0 && api.onRelease) api.onRelease();
    };
    api.held = function () { return n > 0; };
    api.reasons = function () { return Object.keys(held).map(function (k) { return held[k]; }); };
    return api;
  }

  var api = { pageKind: pageKind, wants: wants, scheduler: scheduler, holdCounter: holdCounter };
  if (typeof root !== "undefined" && !(typeof module !== "undefined" && module.exports)) root.OtsLiveHold = holdCounter();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.OtsLiveCore = api;
})(typeof self !== "undefined" ? self : this);
