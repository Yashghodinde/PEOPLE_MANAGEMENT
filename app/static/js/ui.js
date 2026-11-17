/* Alpine components for the design system. Written for the CSP build: logic lives here, templates only
   reference properties and methods (no inline expressions). */
document.addEventListener("alpine:init", function () {
  "use strict";

  function store(key, value) {
    try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (_) {}
    return null;
  }

  Alpine.data("sidebar", function () {
    return {
      collapsed: store("ots.sidebar") === "1",
      mobileOpen: false,
      toggleMobile: function () { this.mobileOpen = !this.mobileOpen; },
      toggle: function () { this.collapsed = !this.collapsed; store("ots.sidebar", this.collapsed ? "1" : "0"); },
      get expanded() { return !this.collapsed; },
      get linkClass() { return this.collapsed && !this.mobileOpen ? "justify-center px-0" : "px-3"; },
      get sidebarClass() {
        var w = (this.collapsed ? "w-sidebar-collapsed p-2" : "w-sidebar p-3");
        return this.mobileOpen ? w + " max-md:flex max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-40 max-md:w-sidebar" : w;
      },
    };
  });

  // Pairing code: counts down from the seconds the server reported at render time (no client clock needed).
  Alpine.data("pairCode", function () {
    return {
      left: 0, copied: false,
      init: function () {
        var self = this;
        this.t0 = Date.now(); this.total = Number(this.$el.dataset.seconds) || 0;
        this.tick();
        this.timer = setInterval(function () { self.tick(); }, 1000);
      },
      tick: function () {
        this.left = Math.max(0, this.total - Math.floor((Date.now() - this.t0) / 1000));
        if (this.left === 0) clearInterval(this.timer);
      },
      get expired() { return this.left <= 0; },
      get live() { return this.left > 0; },
      get codeClass() { return this.left > 0 ? "" : "opacity-30 line-through"; },
      get remaining() { var m = Math.floor(this.left / 60), s = this.left % 60; return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s; },
      copy: function () {
        var self = this;
        if (this.left <= 0 || !navigator.clipboard) return;
        navigator.clipboard.writeText(this.$el.dataset.code).then(function () { self.copied = true; setTimeout(function () { self.copied = false; }, 2000); });
      },
    };
  });

  // Org chart: branches open and close one at a time (class toggle, no re-render); zoom scales the tree and keeps its frame
  // sized to match, so the scroll area, pan and the connector lines (plain borders) stay aligned.
  Alpine.data("orgChart", function () {
    return {
      scale: 1, searchNote: "", drag: null,
      init: function () { var self = this; this.$nextTick(function () { self.fit(); }); },
      get zoomLabel() { return Math.round(this.scale * 100) + "%"; },
      apply: function () {
        var c = this.$refs.canvas, f = this.$refs.frame;
        c.style.transform = "scale(" + this.scale + ")";
        f.style.width = (c.offsetWidth * this.scale + 42) + "px";
        f.style.height = (c.offsetHeight * this.scale + 42) + "px";
      },
      setScale: function (v) { this.scale = Math.min(2, Math.max(0.2, Math.round(v * 100) / 100)); this.apply(); },
      zoomIn: function () { this.setScale(this.scale + 0.1); },
      zoomOut: function () { this.setScale(this.scale - 0.1); },
      fit: function () {
        var c = this.$refs.canvas, v = this.$refs.viewport;
        c.style.transform = "scale(1)";
        this.setScale(Math.min(1, (v.clientWidth - 42) / Math.max(c.offsetWidth, 1)));
        v.scrollLeft = 0; v.scrollTop = 0;
      },
      nodes: function () { return this.$refs.canvas.querySelectorAll("li[data-org-node]"); },
      setOpen: function (li, open) {
        li.classList.toggle("org-collapsed", !open);
        var b = li.querySelector(":scope > [data-org-card] [data-org-toggle]");
        if (b) b.setAttribute("aria-expanded", open ? "true" : "false");
      },
      expandAll: function () { var self = this; this.nodes().forEach(function (li) { self.setOpen(li, true); }); this.apply(); },
      collapseAll: function () {
        var self = this;
        this.nodes().forEach(function (li) { if (li.querySelector(":scope > ul")) self.setOpen(li, false); });
        this.apply();
      },
      onClick: function (e) {
        var b = e.target.closest("[data-org-toggle]");
        if (!b) return;
        var li = b.closest("li[data-org-node]");
        this.setOpen(li, li.classList.contains("org-collapsed"));
        this.apply();
      },
      clearMarks: function () {
        this.$refs.canvas.querySelectorAll(".org-hit,.org-path").forEach(function (el) { el.classList.remove("org-hit", "org-path"); });
      },
      onSearch: function (e) {
        var q = (e.target.value || "").trim().toLowerCase(), v = this.$refs.viewport;
        this.clearMarks();
        if (!q) { this.searchNote = ""; return; }
        var hit = null;
        this.nodes().forEach(function (li) { if (!hit && li.dataset.name.indexOf(q) !== -1) hit = li; });
        if (!hit) { this.searchNote = "No one found."; return; }
        this.searchNote = "";
        var up = hit.parentElement.closest("li[data-org-node]");
        while (up) { this.setOpen(up, true); up.querySelector(":scope > [data-org-card]").classList.add("org-path"); up = up.parentElement.closest("li[data-org-node]"); }
        this.apply();
        var card = hit.querySelector(":scope > [data-org-card]");
        card.classList.add("org-hit");
        var c = this.$refs.canvas.getBoundingClientRect(), r = card.getBoundingClientRect();
        v.scrollTo({
          left: Math.max(0, v.scrollLeft + (r.left - c.left) + r.width / 2 - v.clientWidth / 2 + 21),
          top: Math.max(0, v.scrollTop + (r.top - c.top) + r.height / 2 - v.clientHeight / 2 + 21),
          behavior: "smooth",
        });
      },
      dragStart: function (e) {
        if (e.pointerType === "touch" || e.target.closest("a,button")) return;
        var v = this.$refs.viewport;
        this.drag = { x: e.clientX, y: e.clientY, l: v.scrollLeft, t: v.scrollTop };
        v.classList.add("is-dragging");
      },
      dragMove: function (e) {
        if (!this.drag) return;
        var v = this.$refs.viewport;
        v.scrollLeft = this.drag.l - (e.clientX - this.drag.x);
        v.scrollTop = this.drag.t - (e.clientY - this.drag.y);
      },
      dragEnd: function () { this.drag = null; this.$refs.viewport.classList.remove("is-dragging"); },
    };
  });

  Alpine.data("dropdown", function () {
    return {
      open: false,
      toggle: function () { this.open = !this.open; },
      close: function () { this.open = false; },
    };
  });

  Alpine.data("modal", function () {
    return {
      open: false,
      show: function () { this.open = true; var self = this; this.$nextTick(function () { var el = self.$refs.panel; if (el) el.focus(); }); },
      hide: function () { this.open = false; },
      onOpen: function (e) { if (e.detail && e.detail.id === this.$el.dataset.modalId) this.show(); },
      trap: function (e) {
        var f = this.$refs.panel.querySelectorAll("a[href],button:not([disabled]),input,select,textarea,[tabindex]:not([tabindex='-1'])");
        if (!f.length) return;
        var first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      },
    };
  });

  Alpine.data("drawer", function () {
    return {
      open: false,
      show: function () { this.open = true; },
      hide: function () { this.open = false; },
      onOpen: function (e) { if (e.detail && e.detail.id === this.$el.dataset.drawerId) this.show(); },
    };
  });

  // Tabs: panels are toggled imperatively because the CSP build cannot evaluate calls with arguments.
  Alpine.data("tabs", function () {
    var ON = ["text-ink-900", "border-accent"], OFF = ["text-ink-600", "border-transparent"];
    return {
      root: null,
      show: function (name) {
        this.root.querySelectorAll("[role=tabpanel]").forEach(function (p) { p.hidden = p.dataset.panel !== name; });
        this.root.querySelectorAll("[role=tab]").forEach(function (t) {
          var on = t.dataset.tab === name;
          t.setAttribute("aria-selected", on ? "true" : "false");
          ON.forEach(function (c) { t.classList.toggle(c, on); });
          OFF.forEach(function (c) { t.classList.toggle(c, !on); });
        });
      },
      init: function () { this.root = this.$el; this.show(this.$el.dataset.initial || ""); },
      select: function (e) { this.show(e.currentTarget.dataset.tab); },
    };
  });

  Alpine.data("toastRegion", function () {
    return {
      items: [],
      seq: 0,
      add: function (e) {
        var d = e.detail || {}, id = ++this.seq, self = this;
        this.items.push({ id: id, kind: d.kind || "success", message: d.message || "" });
        if (d.kind !== "error") setTimeout(function () { self.remove(id); }, 5000);
      },
      remove: function (id) { this.items = this.items.filter(function (t) { return t.id !== id; }); },
      dismiss: function (e) { this.remove(Number(e.currentTarget.dataset.id)); },
    };
  });

  Alpine.data("serverToast", function () {
    return {
      init: function () {
        var self = this;
        setTimeout(function () { if (self.$el.dataset.kind !== "error") self.$el.remove(); }, 5000);
      },
      close: function () { this.$el.remove(); },
    };
  });
});

window.otsToast = function (kind, message) {
  window.dispatchEvent(new CustomEvent("ots-toast", { detail: { kind: kind, message: message } }));
};

// Openers: any element with data-open-modal="id" / data-open-drawer="id" (keeps templates free of expressions).
document.addEventListener("click", function (e) {
  var m = e.target.closest("[data-open-modal]");
  if (m) window.dispatchEvent(new CustomEvent("open-modal", { detail: { id: m.dataset.openModal } }));
  var d = e.target.closest("[data-open-drawer]");
  if (d) window.dispatchEvent(new CustomEvent("open-drawer", { detail: { id: d.dataset.openDrawer } }));
});
