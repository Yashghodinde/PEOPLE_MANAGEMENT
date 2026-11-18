/* The task workspace (IMP-15.8): tabs, the side panel, the help dialog and a chat that stays where the reader is.
   CSP build: the template only reads properties and calls methods without arguments. */
document.addEventListener("alpine:init", function () {
  "use strict";
  var TITLES = { people: "People", details: "Details", activity: "Activity", edit: "Edit task", cancel: "Cancel task" };

  Alpine.data("taskWorkspace", function () {
    return {
      tab: "chat", drawer: "", helpOpen: false, helpAssignment: "", atBottom: true,
      init: function () {
        var self = this;
        this.root = this.$el;
        this.$nextTick(function () {
          self.toBottom();
          var box = self.$refs.scroll;
          if (box) box.addEventListener("scroll", function () { self.atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60; });
          var open = self.root.dataset.openRequest;  // a notification opened a particular request: show it
          if (open && box) { var card = box.querySelector('[data-request="' + open + '"]'); if (card) card.scrollIntoView({ block: "center" }); }
        });
      },
      get isChat() { return this.tab === "chat"; },
      get isFiles() { return this.tab === "files"; },
      get chatTab() { return this.tab === "chat" ? "border-accent bg-accent text-canvas font-medium" : "border-line text-ink-900 hover:bg-line"; },
      get filesTab() { return this.tab === "files" ? "border-accent bg-accent text-canvas font-medium" : "border-line text-ink-900 hover:bg-line"; },
      get drawerTitle() { return TITLES[this.drawer] || ""; },
      toBottom: function () { var box = this.$refs.scroll; if (box) box.scrollTop = box.scrollHeight; },
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
        if (e.target.value.trim() && form) form.requestSubmit();
      },
    };
  });
});
