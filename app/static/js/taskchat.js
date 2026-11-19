/* The task workspace (IMP-15.8): tabs, the side panel, the help dialog and a chat that stays where the reader is.
   CSP build: the template only reads properties and calls methods without arguments. */
document.addEventListener("alpine:init", function () {
  "use strict";
  var TITLES = { people: "People", details: "Details", activity: "Activity", edit: "Edit task", cancel: "Cancel task" };

  var reduce = function () { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); };
  function uuid() { return window.otsUuid7 ? window.otsUuid7() : (window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()); }
  function csrfOf(form) { var i = form.querySelector("input[name=csrfmiddlewaretoken]"); return i ? i.value : ""; }

  Alpine.data("taskWorkspace", function () {
    var logEl = null, rootEl = null;   // plain variables: a DOM node kept on Alpine's reactive data becomes a proxy and breaks
    return {
      tab: "chat", drawer: "", helpOpen: false, helpAssignment: "", atBottom: true,
      hasText: false, stagedN: 0, sending: false, newBelow: 0, conn: "connected", connShown: false, connTimer: null,
      init: function () {
        var self = this;
        rootEl = this.$el;
        this.$nextTick(function () {
          var box = rootEl.querySelector("[data-chat-log]");   // not $refs: it sits inside the nested upload component
          logEl = box;
          self.toBottom();
          if (box) box.addEventListener("scroll", function () {
            self.atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
            if (self.atBottom) self.newBelow = 0;
          });
          // the live connection tells a page when something changed: merge new messages in place (never a page swap)
          window.OtsLiveMerge = function () { if (!document.querySelector("[data-chat-log]")) return false; self.refreshLog(); return true; };
          window.addEventListener("ots:staged", function (ev) { if (ev.detail) self.stagedN = ev.detail.n; });
        window.addEventListener("ots:live-state", function (ev) { self.setConn(ev.detail && ev.detail.state); });
          window.addEventListener("online", function () { self.setConn("reconnecting"); });
          window.addEventListener("offline", function () { self.setConn("offline"); });
          var open = rootEl.dataset.openRequest;  // a notification opened a particular request: show it
          if (open && box) { var card = box.querySelector('[data-request="' + open + '"]'); if (card) card.scrollIntoView({ block: "center" }); }
        });
      },
      destroy: function () { if (window.OtsLiveMerge) window.OtsLiveMerge = null; },
      get isChat() { return this.tab === "chat"; },
      get hasNew() { return this.newBelow > 0; },
      get drawerOpen() { return this.drawer !== ""; },
      get showPeople() { return this.drawer === "people"; },
      get showDetails() { return this.drawer === "details"; },
      get showEdit() { return this.drawer === "edit"; },
      get showCancel() { return this.drawer === "cancel"; },
      get showActivity() { return this.drawer === "activity"; },
      get showSend() { return this.hasText || this.stagedN > 0; },
      get showMic() { return !this.showSend; },
      get cantSend() { return !this.showSend || this.sending; },
      get connVisible() { return this.conn !== "connected" || this.connShown; },
      get connText() { return this.conn === "connected" ? "Connected" : this.conn === "offline" ? "Offline" : "Reconnecting..."; },
      get connDot() { return "conn-dot-" + this.conn; },
      get newLabel() { return this.newBelow + (this.newBelow === 1 ? " new message" : " new messages"); },
      setConn: function (state) {
        if (state !== "connected" && state !== "offline") state = "reconnecting";
        if (state === this.conn && state !== "connected") return;
        var was = this.conn, self = this;
        this.conn = state;
        clearTimeout(this.connTimer);
        if (state === "connected" && was !== "connected") {   // "Connected" is shown briefly, only after a gap
          this.connShown = true;
          this.connTimer = setTimeout(function () { self.connShown = false; }, 3000);
          this.refreshLog();                                  // what arrived while the line was down
        } else if (state === "connected") this.connShown = false;
      },
      typed: function (e) { this.hasText = e.target.value.trim() !== ""; },
      jumpToNew: function () { this.newBelow = 0; this.scrollDown(true); },
      scrollDown: function (smooth) {
        var box = logEl;
        if (box) box.scrollTo({ top: box.scrollHeight, behavior: smooth && !reduce() ? "smooth" : "auto" });
      },
      // ---- sending: the message appears at once, then is confirmed by the server ----
      sendMessage: function (e) {
        e.preventDefault();
        var form = e.target, ta = form.querySelector("textarea"), body = ta.value.trim();
        var hasFiles = this.stagedN > 0;
        if ((!body && !hasFiles) || this.sending) return;
        if (hasFiles) form.dispatchEvent(new CustomEvent("ots-send-staged", { bubbles: true }));   // the chosen files go with the message
        if (!body) return;
        var log = logEl, self = this, key = uuid();
        var row = document.createElement("div");
        row.className = "flex " + (log.dataset.sideMine === "right" ? "justify-end" : "justify-start") + " motion-msg-in";
        row.setAttribute("data-pending", "1"); row.setAttribute("data-body", body);
        row.innerHTML = '<div class="bubble max-w-[80%] min-w-0 border border-line p-3 flex flex-col gap-1 opacity-70"><p class="text-cap font-semibold text-ink-900"></p><p class="text-body text-ink-900 whitespace-pre-line break-words"></p><p class="text-cap text-ink-600 text-right" data-state>Sending...</p></div>';
        row.querySelector("p").textContent = log.dataset.me || "";
        row.querySelectorAll("p")[1].textContent = body;
        var empty = log.querySelector("p.self-center"); if (empty) empty.remove();
        log.appendChild(row); this.scrollDown(false);
        ta.value = ""; this.hasText = false;
        var data = new FormData(form); data.set("body", body);
        function attempt() {
          self.sending = true;
          fetch(form.getAttribute("action"), { method: "POST", body: data, credentials: "same-origin",
            headers: { "X-Requested-With": "ots-chat", "Idempotency-Key": key, "X-CSRFToken": csrfOf(form), Accept: "application/json" } })
            .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return [r.status, j]; }); })
            .then(function (res) {
              self.sending = false;
              if (res[0] === 200 && res[1].ok) {
                var bubble = row.firstElementChild; bubble.classList.remove("opacity-70", "bubble-failed");
                row.querySelector("[data-state]").textContent = "Sent";
                row.dataset.confirmed = res[1].id; self.refreshLog();
              } else fail(res[1] && res[1].message ? res[1].message : "Not sent.");
            }, function () { self.sending = false; fail("Not sent. Check the connection."); });
        }
        function fail(text) {
          var bubble = row.firstElementChild; bubble.classList.add("bubble-failed");
          var st = row.querySelector("[data-state]"); st.textContent = text + " ";
          var btn = document.createElement("button"); btn.type = "button"; btn.textContent = "Retry";
          btn.className = "text-accent font-medium"; btn.addEventListener("click", function () { btn.remove(); st.textContent = "Sending..."; bubble.classList.remove("bubble-failed"); attempt(); });
          st.appendChild(btn);
        }
        attempt();
      },
      // ---- new messages: merged into the open conversation, never a reload, never a jump ----
      refreshLog: function () {
        var self = this, box = logEl;
        if (!box || this.merging) return;
        this.merging = true;
        fetch(box.dataset.url, { credentials: "same-origin", headers: { "X-Requested-With": "ots-live" } }).then(function (r) {
          if (r.status === 401) { window.location.href = "/login?reason=ended"; return ""; }
          return r.ok ? r.text() : "";
        }).then(function (html) {
          self.merging = false;
          if (!html) return;
          var doc = new DOMParser().parseFromString(html, "text/html"), fresh = doc.querySelector("[data-chat-log]");
          if (!fresh) return;
          var wasAtBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60, added = 0, others = 0;
          Array.prototype.forEach.call(fresh.children, function (node) {
            var mid = node.getAttribute("data-mid");
            if (!mid) return;
            var have = box.querySelector('[data-mid="' + mid + '"]');
            if (have) { if (have.getAttribute("data-sig") !== node.getAttribute("data-sig")) have.replaceWith(document.importNode(node, true)); return; }
            var clone = document.importNode(node, true), mine = node.querySelector("p.text-body") ? node.querySelector("p.text-body").textContent.trim() : "";
            var pending = box.querySelector('[data-pending][data-confirmed="' + mid + '"], [data-pending][data-body]');
            if (pending && pending.getAttribute("data-body") === mine && node.getAttribute("data-side") === box.dataset.sideMine) {
              pending.replaceWith(clone); return;   // my own message, already on screen: confirmed in place, no second animation
            }
            if (!reduce()) clone.classList.add("motion-msg-in");
            box.appendChild(clone); added++;
            if (node.getAttribute("data-side") !== box.dataset.sideMine && node.getAttribute("data-side") !== "center") others++;
          });
          ["header", "actions"].forEach(function (name) {   // status, buttons and receipts follow the server
            var a = document.querySelector('[data-live-region="' + name + '"]'), b = doc.querySelector('[data-live-region="' + name + '"]');
            if (a && b && !a.contains(document.activeElement) && a.innerHTML !== b.innerHTML) a.innerHTML = b.innerHTML;
          });
          if (added && wasAtBottom) self.scrollDown(true);
          else if (others) self.newBelow += others;
        }, function () { self.merging = false; });
      },
      get isFiles() { return this.tab === "files"; },
      get chatTab() { return this.tab === "chat" ? "border-accent bg-accent text-canvas font-medium" : "border-line text-ink-900 hover:bg-line"; },
      get filesTab() { return this.tab === "files" ? "border-accent bg-accent text-canvas font-medium" : "border-line text-ink-900 hover:bg-line"; },
      get drawerTitle() { return TITLES[this.drawer] || ""; },
      toBottom: function () { if (logEl) logEl.scrollTop = logEl.scrollHeight; },
      showChat: function () { this.tab = "chat"; },   // the scroll box keeps its place: it is only hidden
      showFiles: function () { this.tab = "files"; },
      openPeople: function () { this.drawer = "people"; },
      openDetails: function () { this.drawer = "details"; },
      openActivity: function () { this.drawer = "activity"; },
      openEdit: function () { this.drawer = "edit"; },
      openCancel: function () { this.drawer = "cancel"; },
      closeDrawer: function () { this.drawer = ""; this.helpOpen = false; },
      openHelp: function (e) { this.helpAssignment = e.currentTarget.dataset.assignment; this.helpOpen = true; },
      closeHelp: function () { this.helpOpen = false; },
      // Enter sends, Shift+Enter is a new line
      enterSends: function (e) {
        if (e.shiftKey || e.isComposing) return;
        e.preventDefault();
        var form = e.target.closest("form");
        if ((e.target.value.trim() || this.stagedN > 0) && form) form.requestSubmit();
      },
    };
  });
});
