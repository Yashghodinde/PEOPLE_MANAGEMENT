/* The shared file manager (IMP-14.3): view and sort memory, the details panel, and the small dialogs.
   CSP build: no expressions in templates, so every value a template reads is a property or method here. */
document.addEventListener("alpine:init", function () {
  "use strict";

  function get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  var SORTS = {
    name_asc: function (a, b) { return a.dataset.name.localeCompare(b.dataset.name); },
    name_desc: function (a, b) { return b.dataset.name.localeCompare(a.dataset.name); },
    mod_desc: function (a, b) { return Number(b.dataset.mod) - Number(a.dataset.mod); },
    mod_asc: function (a, b) { return Number(a.dataset.mod) - Number(b.dataset.mod); },
    size_desc: function (a, b) { return Number(b.dataset.size) - Number(a.dataset.size); },
    size_asc: function (a, b) { return Number(a.dataset.size) - Number(b.dataset.size); },
    type: function (a, b) { return a.dataset.type.localeCompare(b.dataset.type) || a.dataset.name.localeCompare(b.dataset.name); },
  };

  Alpine.data("fileManager", function () {
    return {
      view: "list", sort: "name_asc", detailsOpen: false, details: [], selected: null,
      newOpen: false, renameOpen: false, renameAction: "", renameName: "", renameKind: "",
      moveOpen: false, moveKind: "", moveId: "", hideOpen: false, hideAction: "", hideName: "",
      init: function () {
        this.key = "ots.fm." + (this.$el.dataset.key || "x");
        var v = get(this.key + ".view"), s = get(this.key + ".sort");
        this.view = v === "grid" ? "grid" : "list";
        this.sort = SORTS[s] ? s : (this.$el.dataset.defaultSort || "name_asc");
        var self = this;
        this.$nextTick(function () { self.applySort(); });
      },
      get isList() { return this.view === "list"; },
      get isGrid() { return this.view === "grid"; },
      get listBtn() { return this.view === "list" ? "bg-line font-medium" : ""; },
      get gridBtn() { return this.view === "grid" ? "bg-line font-medium" : ""; },
      get headClass() { return this.view === "list" ? "" : "hidden"; },
      get anyDialog() { return this.newOpen || this.renameOpen || this.moveOpen || this.hideOpen; },
      get noSelection() { return this.details.length === 0; },
      setList: function () { this.view = "list"; set(this.key + ".view", "list"); },
      setGrid: function () { this.view = "grid"; set(this.key + ".view", "grid"); },
      onSort: function (e) { this.sort = e.target.value; set(this.key + ".sort", this.sort); this.applySort(); },
      applySort: function () {
        var list = this.$refs.list;
        if (!list) return;
        var rows = Array.prototype.slice.call(list.querySelectorAll(":scope > [data-item]")), fn = SORTS[this.sort];
        rows.sort(function (a, b) {  // folders stay above files, whatever the sort
          var fa = a.dataset.kind === "folder" ? 0 : 1, fb = b.dataset.kind === "folder" ? 0 : 1;
          return fa !== fb ? fa - fb : fn(a, b);
        });
        rows.forEach(function (r) { list.appendChild(r); });
      },
      toggleDetails: function () { this.detailsOpen = !this.detailsOpen; },
      closeDetails: function () { this.detailsOpen = false; },
      show: function (li) {
        var d = {};
        try { d = JSON.parse(li.dataset.details || "{}"); } catch (e) {}
        this.details = Object.keys(d).map(function (k) { return { k: k, v: d[k] }; });
      },
      select: function (e) {
        if (e.target.closest("a,button,summary,input,form")) return;
        this.pick(e.currentTarget);
      },
      pick: function (li) {
        if (this.selected) this.selected.setAttribute("aria-selected", "false");
        this.selected = li; li.setAttribute("aria-selected", "true");
        this.show(li);
      },
      openDetails: function (e) {
        this.pick(e.currentTarget.closest("[data-item]"));
        this.detailsOpen = true;
        e.currentTarget.closest("details").removeAttribute("open");
      },
      openNew: function () { this.newOpen = true; var self = this; this.$nextTick(function () { self.$refs.newName.focus(); }); },
      openRename: function (e) {
        var li = e.currentTarget.closest("[data-item]");
        this.renameKind = li.dataset.kind;
        this.renameAction = li.dataset.rename;
        this.renameName = li.dataset.title; this.renameOpen = true;
        e.currentTarget.closest("details").removeAttribute("open");
      },
      openMove: function (e) {
        var li = e.currentTarget.closest("[data-item]");
        this.moveKind = li.dataset.kind; this.moveId = li.dataset.id; this.moveOpen = true;
        e.currentTarget.closest("details").removeAttribute("open");
      },
      openHide: function (e) {
        var li = e.currentTarget.closest("[data-item]");
        this.hideAction = li.dataset.hide; this.hideName = li.dataset.title; this.hideOpen = true;
        e.currentTarget.closest("details").removeAttribute("open");
      },
      closeAll: function () { this.newOpen = this.renameOpen = this.moveOpen = this.hideOpen = false; },
    };
  });
});
