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
      get sidebarClass() {
        var w = this.collapsed ? "w-sidebar-collapsed" : "w-sidebar";
        return this.mobileOpen ? w + " max-md:flex max-md:fixed max-md:inset-y-0 max-md:left-0 max-md:z-40 max-md:w-sidebar" : w;
      },
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
