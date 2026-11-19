/* Office Chat (IMP-16.9 to 16.12). Alpine for the few things the page itself shows (dialogs, the typing line, the
   chip); plain delegated handlers for every per-message button. CSP build: templates only read properties and call
   methods without arguments, and no DOM node is kept on reactive data (it becomes a proxy and breaks). */
document.addEventListener("alpine:init", function () {
  "use strict";
  var reduce = function () { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); };
  function uuid() { return window.otsUuid7 ? window.otsUuid7() : (window.crypto && crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()); }
  function csrf() { try { return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRFToken"] || ""; } catch (e) { return ""; } }
  function post(url, body, extra) {
    return fetch(url, { method: "POST", credentials: "same-origin", body: JSON.stringify(body || {}),
      headers: Object.assign({ "Content-Type": "application/json", "X-CSRFToken": csrf(), Accept: "application/json", "Idempotency-Key": (body && body.client_id) || uuid() }, extra || {}) })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return [r.status, j]; }); });
  }
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; }

  Alpine.data("officeChat", function () {
    var logEl = null, rootEl = null, chosen = {}, replyTo = "", editId = "", typingTimers = {}, lastTypingSent = 0, merging = false, readTimer = null, searchTimer = null;
    return {
      newOpen: false, newError: "", selCount: 0, detailsOpen: false, hasText: false, sending: false, newBelow: 0,
      typingNames: "", pickOpen: false, pickTitle: "", replyLine: "",
      init: function () {
        var self = this;
        rootEl = this.$el;
        this.$nextTick(function () {
          logEl = rootEl.querySelector("[data-chat-log]");
          if (logEl) {
            logEl.scrollTop = logEl.scrollHeight;
            logEl.addEventListener("scroll", function () { if (self.atBottom()) { self.newBelow = 0; self.queueRead(); } });
            logEl.addEventListener("click", function (e) { self.onLogClick(e); });
            var comp = rootEl.querySelector("[data-composer]");
            rootEl.addEventListener("click", function (e) { self.onRootClick(e); });
            rootEl.addEventListener("change", function (e) { self.onChange(e); });
            rootEl.addEventListener("submit", function (e) { if (e.target.hasAttribute("data-rename-form")) { e.preventDefault(); self.manage({ op: "rename", title: e.target.elements.title.value }, true); } });
            rootEl.addEventListener("input", function (e) { if (e.target.hasAttribute && e.target.hasAttribute("data-add-search")) self.searchAdd(e.target.value); });
            self.queueRead();
            void comp;
          }
        });
        window.addEventListener("ots:chat", function (ev) { self.onLive(ev.detail || {}); });
        window.addEventListener("ots:chat-file", function (ev) { self.sendFile(ev.detail || {}); });
        document.addEventListener("visibilitychange", function () { if (!document.hidden) self.queueRead(); });
      },
      destroy: function () { logEl = null; },
      // ---- state the template reads ----
      get convId() { return rootEl ? rootEl.dataset.conv : ""; },
      get isGroup() { return this.selCount > 1; },
      get cantStart() { return this.selCount === 0; },
      get startLabel() { return this.selCount > 1 ? "Create group" : "Open chat"; },
      get hasNew() { return this.newBelow > 0; },
      get newLabel() { return this.newBelow + (this.newBelow === 1 ? " new message" : " new messages"); },
      get cantSend() { return !this.hasText || this.sending; },
      get typingShown() { return this.typingNames !== ""; },
      get typingText() { return this.typingNames + " typing"; },
      get replying() { return replyTo !== "" || editId !== ""; },
      get replyText() { return this.replyLine; },
      get detailsShown() { return this.detailsOpen; },
      atBottom: function () { return !logEl || logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 60; },
      toggleDetails: function () { this.detailsOpen = !this.detailsOpen; },
      jumpToNew: function () { this.newBelow = 0; this.scrollDown(true); },
      scrollDown: function (smooth) { if (logEl) logEl.scrollTo({ top: logEl.scrollHeight, behavior: smooth && !reduce() ? "smooth" : "auto" }); },
      typed: function (e) {
        this.hasText = e.target.value.trim() !== "";
        var t = Date.now();
        if (this.hasText && t - lastTypingSent > 3000 && this.convId) { lastTypingSent = t; post("/app/chat/c/" + this.convId + "/typing", {}); }
      },
      enterSends: function (e) {
        if (e.shiftKey || e.isComposing) return;
        e.preventDefault();
        var form = e.target.closest("form");
        if (e.target.value.trim() && form) form.requestSubmit();
      },
      cancelReply: function () { replyTo = ""; editId = ""; this.replyLine = ""; },
      // ---- new chat dialog ----
      openNew: function () {
        var self = this; this.newOpen = true; this.newError = ""; chosen = {}; this.selCount = 0;
        this.$nextTick(function () { self.$refs.peopleSearch.value = ""; self.$refs.peopleSearch.focus(); self.loadPeople(""); });
      },
      closeNew: function () { this.newOpen = false; },
      searchPeople: function (e) { var v = e.target.value; clearTimeout(searchTimer); var self = this; searchTimer = setTimeout(function () { self.loadPeople(v); }, 250); },
      loadPeople: function (q) {
        var self = this, ul = rootEl.querySelector("[data-people-list]");
        fetch("/app/chat/people?q=" + encodeURIComponent(q), { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
          ul.textContent = "";
          if (!d.people.length) { ul.appendChild(el("li", "p-3 text-body text-ink-600", "Nobody matches.")); return; }
          d.people.forEach(function (p) {
            var li = el("li", "flex items-center gap-3 p-3"), cb = document.createElement("input");
            cb.type = "checkbox"; cb.id = "np-" + p.id; cb.checked = !!chosen[p.id];
            cb.addEventListener("change", function () { if (cb.checked) chosen[p.id] = p.name; else delete chosen[p.id]; self.selCount = Object.keys(chosen).length; });
            var lab = el("label", "flex-1 cursor-pointer"); lab.setAttribute("for", cb.id);
            lab.appendChild(el("span", "block text-body text-ink-900", p.name)); lab.appendChild(el("span", "block text-cap text-ink-600", p.designation + " · " + p.employee_id));
            li.appendChild(cb); li.appendChild(lab); ul.appendChild(li);
          });
        });
      },
      startChat: function () {
        var self = this, ids = Object.keys(chosen), title = this.$refs.groupTitle ? this.$refs.groupTitle.value : "";
        post("/app/chat/new", { people: ids, title: title }).then(function (res) {
          if (res[0] === 201 && res[1].ok) { window.location.href = res[1].url; } else self.newError = (res[1] && res[1].message) || "Could not start the chat.";
        });
      },
      // ---- sending ----
      sendMessage: function (e) {
        e.preventDefault();
        var form = e.target, ta = form.querySelector("textarea"), body = ta.value.trim();
        if (!body || this.sending || !logEl) return;
        if (editId) {   // the message being edited is saved in place; nothing new is posted
          var self = this, id = editId;
          post("/app/chat/m/" + id + "/edit", { body: body }).then(function (res) {
            if (res[1].ok) { ta.value = ""; self.hasText = false; self.cancelReply(); self.refreshLog(); } else self.replyLine = res[1].message || "Could not edit.";
          });
          return;
        }
        this.postMessage({ body: body, reply_to: replyTo || null, client_id: uuid() }, body);
        ta.value = ""; this.hasText = false; this.cancelReply();
      },
      sendFile: function (d) {
        if (!d.versionId || d.conversationId !== this.convId) return;
        this.postMessage({ version_ids: [d.versionId], body: "", client_id: uuid() }, "");
      },
      postMessage: function (payload, shown) {
        var self = this, row = el("div", "flex justify-end motion-msg-in");
        row.setAttribute("data-pending", "1"); row.setAttribute("data-body", shown);
        var bub = el("div", "bubble border border-line bg-line max-w-[78%] min-w-0 p-3 flex flex-col gap-1 opacity-70");
        bub.appendChild(el("p", "text-body text-ink-900 whitespace-pre-line break-words", shown || "Sending file..."));
        var st = el("p", "text-cap text-ink-600 text-right", "Sending..."); bub.appendChild(st); row.appendChild(bub);
        var empty = logEl.querySelector("[data-empty]"); if (empty) empty.remove();
        logEl.appendChild(row); this.scrollDown(false);
        function attempt() {
          self.sending = true;
          post("/app/chat/c/" + self.convId + "/send", payload).then(function (res) {
            self.sending = false;
            if ((res[0] === 200 || res[0] === 201) && res[1].ok) { row.dataset.confirmed = res[1].id; bub.classList.remove("opacity-70"); st.textContent = "Sent"; self.refreshLog(); }
            else fail((res[1] && res[1].message) || "Not sent.");
          }, function () { self.sending = false; fail("Not sent. Check the connection."); });
        }
        function fail(text) {
          bub.classList.add("bubble-failed"); st.textContent = text + " ";
          var b = el("button", "text-accent font-medium", "Retry"); b.type = "button";
          b.addEventListener("click", function () { b.remove(); st.textContent = "Sending..."; bub.classList.remove("bubble-failed"); attempt(); });
          st.appendChild(b);
        }
        attempt();
      },
      // ---- merging what the server has (new messages, edits, reactions, read marks) without a reload ----
      refreshLog: function () {
        var self = this;
        if (!logEl || merging) return;
        merging = true;
        fetch(logEl.dataset.url, { credentials: "same-origin", headers: { "X-Requested-With": "ots-live" } }).then(function (r) { return r.ok ? r.text() : ""; }).then(function (html) {
          merging = false;
          if (!html) return;
          var tpl = document.createElement("template"); tpl.innerHTML = html;
          var wasBottom = self.atBottom(), added = 0, others = 0;
          Array.prototype.forEach.call(tpl.content.children, function (node) {
            var mid = node.getAttribute("data-mid");
            if (!mid) { return; }
            var have = logEl.querySelector('[data-mid="' + mid + '"]');
            if (have) { if (have.getAttribute("data-sig") !== node.getAttribute("data-sig")) have.replaceWith(document.importNode(node, true)); return; }
            var body = node.querySelector("[data-body]") ? node.querySelector("[data-body]").textContent.trim() : "";
            var pend = logEl.querySelector('[data-pending][data-confirmed="' + mid + '"]') || (node.getAttribute("data-mine") === "1" ? logEl.querySelector('[data-pending][data-body="' + CSS.escape(body) + '"]') : null);
            var clone = document.importNode(node, true);
            if (pend) { pend.replaceWith(clone); return; }
            if (!reduce()) clone.classList.add("motion-msg-in");
            var empty = logEl.querySelector("[data-empty]"); if (empty) empty.remove();
            logEl.appendChild(clone); added++;
            if (node.getAttribute("data-mine") === "0") others++;
          });
          if (added && wasBottom) { self.scrollDown(true); self.queueRead(); } else if (others) self.newBelow += others;
        }, function () { merging = false; });
      },
      queueRead: function () {
        var self = this; clearTimeout(readTimer);
        readTimer = setTimeout(function () {
          if (document.hidden || !self.convId || !self.atBottom()) return;
          post("/app/chat/c/" + self.convId + "/read", {}).then(function (res) { if (res[1] && res[1].unread !== undefined) window.dispatchEvent(new CustomEvent("ots:chat-badge", { detail: { count: res[1].unread } })); self.refreshList(); });
        }, 600);
      },
      refreshList: function () {
        var box = document.getElementById("chat-list");
        if (!box) return;
        fetch("/app/chat/list?active=" + encodeURIComponent(this.convId) + window.location.search.replace("?", "&"), { credentials: "same-origin" }).then(function (r) { return r.ok ? r.text() : ""; })
          .then(function (html) { if (html) box.innerHTML = html; });
      },
      onLive: function (d) {
        if (d.what === "typing") { if (d.conversation_id === this.convId) this.someoneTyping(d.who || ""); return; }
        this.refreshList();
        if (d.conversation_id === this.convId) { this.refreshLog(); if (d.what === "member") { /* the details panel is rebuilt on the next visit */ } }
      },
      someoneTyping: function (name) {
        var self = this; if (!name) return;
        clearTimeout(typingTimers[name]);
        typingTimers[name] = setTimeout(function () { delete typingTimers[name]; self.typingNames = Object.keys(typingTimers).join(", "); }, 4000);
        this.typingNames = Object.keys(typingTimers).join(", ");
      },
      // ---- per-message buttons (delegated) ----
      onLogClick: function (e) {
        var btn = e.target.closest("[data-action]");
        if (!btn) return;
        var action = btn.getAttribute("data-action"), row = btn.closest("[data-mid]"), mid = row ? row.getAttribute("data-mid") : "", self = this;
        if (action === "older") {
          var first = logEl.querySelector("[data-mid]");
          fetch(logEl.dataset.url + "?before=" + (first ? first.getAttribute("data-mid") : ""), { credentials: "same-origin" }).then(function (r) { var more = r.headers.get("X-Has-More") === "1"; return r.text().then(function (t) { return [more, t]; }); })
            .then(function (res) {
              var tpl = document.createElement("template"), h0 = logEl.scrollHeight, anchor = btn.nextSibling;
              tpl.innerHTML = res[1];
              while (tpl.content.firstChild) logEl.insertBefore(tpl.content.firstChild, anchor);
              logEl.scrollTop += logEl.scrollHeight - h0;   // keep the reader where they were
              if (!res[0]) btn.remove();
            });
        } else if (action === "goto") {
          var t = logEl.querySelector('[data-mid="' + btn.getAttribute("data-target") + '"]');
          if (t) { t.scrollIntoView({ block: "center", behavior: reduce() ? "auto" : "smooth" }); t.classList.add("motion-fade"); }
        } else if (action === "reply") {
          replyTo = mid; var b = row.querySelector("[data-body]"); this.replyLine = "Replying to: " + (b ? b.textContent.trim().slice(0, 80) : "a file"); rootEl.querySelector("[data-composer] textarea").focus();
        } else if (action === "react") {
          post("/app/chat/m/" + mid + "/react", { emoji: btn.getAttribute("data-emoji") }).then(function () { self.refreshLog(); });
          var d = btn.closest("details"); if (d) d.removeAttribute("open");
        } else if (action === "edit") {
          var body = row.querySelector("[data-body]"), now = body ? body.textContent.trim() : "", ta = rootEl.querySelector("[data-composer] textarea");
          ta.value = now; this.hasText = !!now; ta.focus(); editId = mid; replyTo = ""; this.replyLine = "Editing your message";
        } else if (action === "delete") {
          if (window.confirm("Delete this message? It disappears for everyone in the chat.")) post("/app/chat/m/" + mid + "/delete", {}).then(function () { self.refreshLog(); });
        }
      },
      onRootClick: function (e) {
        var self = this, pick = e.target.closest("[data-pick]"), s = e.target.closest("[data-setting]");
        if (pick) { this.openPick(pick.getAttribute("data-pick")); return; }
        var it = e.target.closest("[data-pick-item]");
        if (it) { this.pickOpen = false; this.postMessage({ version_ids: [it.getAttribute("data-pick-item")], body: "", client_id: uuid() }, ""); return; }
        var add = e.target.closest("[data-add-user]");
        if (add) { this.manage({ op: "add", people: [add.getAttribute("data-add-user")], history: !!(rootEl.querySelector("[data-add-history]") || {}).checked }, true); return; }
        var start = e.target.closest("[data-start-with]");
        if (start) { post("/app/chat/new", { people: [start.getAttribute("data-start-with")] }).then(function (res) { if (res[1].ok) window.location.href = res[1].url; }); return; }
        if (!s) return;
        var op = s.getAttribute("data-setting");
        if (op === "remove") { var li = s.closest("[data-member]"); this.manage({ op: "remove", user: li.getAttribute("data-member") }, true); }
        else if (op === "leave") { if (window.confirm("Leave this group?")) this.manage({ op: "leave" }, false, "/app/chat"); }
        else if (op === "archive") { this.manage({ op: "archive", on: true }, false, "/app/chat"); }
        void self;
      },
      onChange: function (e) {
        var s = e.target.closest ? e.target.closest("[data-setting]") : null;
        if (!s) return;
        var op = s.getAttribute("data-setting");
        if (op === "mute" || op === "pin") this.manage({ op: op, on: s.checked }, false);
      },
      manage: function (body, reload, goTo) {
        var self = this;
        post("/app/chat/c/" + this.convId + "/manage", body).then(function (res) {
          if (res[1].ok) { if (goTo) window.location.href = goTo; else if (reload) window.location.reload(); else self.refreshList(); }
          else if (window.otsToast) window.otsToast("error", res[1].message || "That did not work.");
        });
      },
      searchAdd: function (q) {
        clearTimeout(searchTimer);
        var ul = rootEl.querySelector("[data-add-list]");
        searchTimer = setTimeout(function () {
          fetch("/app/chat/people?q=" + encodeURIComponent(q), { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
            var inGroup = {}; Array.prototype.forEach.call(rootEl.querySelectorAll("[data-member]"), function (m) { inGroup[m.getAttribute("data-member")] = 1; });
            ul.textContent = "";
            d.people.filter(function (p) { return !inGroup[p.id]; }).forEach(function (p) {
              var li = el("li", "p-2"), b = el("button", "text-left text-body text-ink-900", p.name + " · " + p.designation); b.type = "button"; b.setAttribute("data-add-user", p.id); li.appendChild(b); ul.appendChild(li);
            });
          });
        }, 250);
      },
      openPick: function (from) {
        var self = this, ul = rootEl.querySelector("[data-pick-list]");
        this.pickTitle = from === "fileid" ? "Attach a File ID document" : "Attach from My storage"; this.pickOpen = true;
        ul.textContent = ""; ul.appendChild(el("li", "p-3 text-body text-ink-600", "Loading..."));
        fetch("/app/chat/attachable?from=" + from, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
          ul.textContent = "";
          if (!d.items.length) { ul.appendChild(el("li", "p-3 text-body text-ink-600", "Nothing to attach here.")); return; }
          d.items.forEach(function (it) {
            var li = el("li"), b = el("button", "w-full text-left p-3 hover:bg-line"); b.type = "button"; b.setAttribute("data-pick-item", it.version);
            b.appendChild(el("span", "block text-body text-ink-900 break-words", it.name)); b.appendChild(el("span", "block text-cap text-ink-600", it.where)); li.appendChild(b); ul.appendChild(li);
          });
        });
        void self;
      },
      closePick: function () { this.pickOpen = false; },
    };
  });
});
