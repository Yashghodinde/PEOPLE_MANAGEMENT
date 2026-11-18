/* Alpine component for the files panel (M08.3). CSP build: no inline expressions, so everything the template
   reads is a property or method here. The queue itself is in upload-core.js. */
document.addEventListener("alpine:init", function () {
  "use strict";

  function csrf() {
    try { return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRFToken"] || ""; } catch (e) { return ""; }
  }
  function storage() {
    return {
      get: function (k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
      set: function (k, v) { try { localStorage.setItem(k, v); } catch (e) {} },
      remove: function (k) { try { localStorage.removeItem(k); } catch (e) {} },
    };
  }
  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    var b = crypto.getRandomValues(new Uint8Array(16)); b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    var h = Array.prototype.map.call(b, function (x) { return ("0" + x.toString(16)).slice(-2); }).join("");
    return [h.slice(0, 8), h.slice(8, 12), h.slice(12, 16), h.slice(16, 20), h.slice(20)].join("-");
  }
  // SHA-256 in a Web Worker; if workers are unavailable, in slices on the main thread (slower, still bounded memory).
  function hashFile(file, progress) {
    return new Promise(function (resolve, reject) {
      if (window.Worker) {
        var w = new Worker("/static/js/hash-worker.js");
        w.onmessage = function (m) {
          if (m.data.progress !== undefined) progress(m.data.progress);
          else { w.terminate(); if (m.data.error) reject(new Error(m.data.error)); else resolve(m.data.sha256); }
        };
        w.onerror = function (e) { w.terminate(); reject(new Error(e.message || "hash worker failed")); };
        w.postMessage({ file: file });
        return;
      }
      var h = new Sha256(), pos = 0, SLICE = 8 * 1024 * 1024;
      (function next() {
        if (pos >= file.size) return resolve(h.hex());
        file.slice(pos, pos + SLICE).arrayBuffer().then(function (buf) {
          h.update(new Uint8Array(buf)); pos += buf.byteLength; progress(pos / file.size); setTimeout(next, 0);
        }, reject);
      })();
    });
  }

  // Ask the server if a name is acceptable (it writes nothing). If this cannot be answered the upload still goes
  // ahead, because the server judges the name again when the upload starts.
  function checkName(name, taskId) {
    return fetch("/files/check-name", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf(), Accept: "application/json" },
      body: JSON.stringify({ name: name, task_id: taskId || null }),
    }).then(function (r) { if (!r.ok) throw new Error("check failed"); return r.json(); });
  }

  // Read dropped files and folders (directory entries, depth first) into [{file, path}] with each file's relative path.
  function walk(entries) {
    var out = [];
    function readAll(reader) {
      return new Promise(function (resolve, reject) {
        var all = [];
        (function more() { reader.readEntries(function (batch) { if (!batch.length) resolve(all); else { all = all.concat(Array.prototype.slice.call(batch)); more(); } }, reject); })();
      });
    }
    function visit(en, prefix) {
      if (en.isFile) return new Promise(function (resolve) { en.file(function (f) { out.push({ file: f, path: prefix + f.name }); resolve(); }, function () { resolve(); }); });
      if (!en.isDirectory) return Promise.resolve();
      return readAll(en.createReader()).then(function (kids) {
        return Promise.all(kids.map(function (k) { return visit(k, prefix + en.name + "/"); }));
      });
    }
    return Promise.all(entries.map(function (en) { return visit(en, ""); })).then(function () { return out; });
  }

  window.OtsUploadEnv = { csrf: csrf, storage: storage, uuid: uuid, hashFile: hashFile }; // voice.js reuses these

  Alpine.data("uploader", function () {
    return {
      items: [], minimised: false, relation: "SUBMISSION", fileId: "", note: "", dragging: false, queue: null,
      init: function () {
        var self = this, d = this.$el.dataset; this.root = this.$el; // handlers on inner inputs see their own $el, so keep the root
        this.taskId = d.taskId; this.scope = d.scope || ""; this.workspaceId = d.workspaceId || ""; this.folderId = d.folderId || ""; this.locked = d.locked === "1"; // scope "personal" = into My storage, no task
        this.relation = d.defaultRelation || "SUBMISSION";
        this.queue = OtsUploadCore.create({
          fetch: window.fetch.bind(window), hashFile: hashFile, uuid: uuid, storage: storage(), csrf: csrf,
          concurrency: 2, checkName: checkName,
          onChange: function (list) {
            self.items = list;
            // the page must not swap itself under a running upload (live.js looks at the hold counter)
            var busy = list.some(function (i) { return i.active; });
            // a folder view shows what just arrived once everything finished well (a failure stays on screen to be read)
            if (self.refresh && !self.reloading && list.length && list.every(function (i) { return i.finished; }) &&
                list.some(function (i) { return i.ok; }) && !list.some(function (i) { return i.failed; })) {
              self.reloading = true; setTimeout(function () { location.reload(); }, 900);
            }
            if (busy && !self.hold && window.OtsLiveHold) self.hold = window.OtsLiveHold.acquire("uploading");
            else if (!busy && self.hold && window.OtsLiveHold) { window.OtsLiveHold.release(self.hold); self.hold = 0; }
          },
          onDone: function () { document.body.dispatchEvent(new CustomEvent("ots:file-uploaded")); },
        });
      },
      get dropClass() { return this.dragging ? "border-accent bg-line" : "border-line"; },
      get hasItems() { return this.items.length > 0; },
      get anyFinished() { return this.items.some(function (i) { return i.finished; }); },
      take: function (fileList, paths) {
        if (this.locked) { if (window.otsToast) window.otsToast("warning", "Files can only be added to Today, or inside a folder."); return; }
        var meta = { taskId: this.taskId, relation: this.relation, fileId: this.fileId, scope: this.scope, workspaceId: this.workspaceId, folderId: this.folderId, note: this.note };
        for (var i = 0; i < fileList.length; i++) {
          var f = fileList[i], rel = paths ? paths[i] : (f.webkitRelativePath || "");
          this.queue.add(f, Object.assign({}, meta, { relPath: rel }));
        }
        this.refresh = this.root.dataset.refresh === "1";
        this.fileId = ""; this.note = ""; this.relation = this.root.dataset.defaultRelation || "SUBMISSION";
      },
      picked: function (e) { this.take(e.target.files); e.target.value = ""; },
      dropped: function (e) {
        this.dragging = false;
        this.fileId = ""; // dropped files are new files, even if "Upload a new version" was clicked and then cancelled
        if (!e.dataTransfer) return;
        var self = this, items = e.dataTransfer.items, entries = [];
        if (items && items.length && items[0].webkitGetAsEntry) {  // folders keep their tree
          for (var i = 0; i < items.length; i++) { var en = items[i].webkitGetAsEntry(); if (en) entries.push(en); }
          if (!entries.length) { this.take(e.dataTransfer.files); return; }
          walk(entries).then(function (found) { self.take(found.map(function (x) { return x.file; }), found.map(function (x) { return x.path; })); });
        } else this.take(e.dataTransfer.files);
      },
      pickedDir: function (e) { this.take(e.target.files); e.target.value = ""; },
      chooseDir: function () {
        var probe = document.createElement("input");
        if (!("webkitdirectory" in probe)) { if (window.otsToast) window.otsToast("warning", "This browser cannot upload a whole folder. Choose files instead."); return; }
        this.fileId = ""; this.$refs.dir.click();
      },
      get trayClass() { return this.minimised ? "hidden" : "flex flex-col gap-2"; },
      get trayLabel() { return this.minimised ? "Show uploads" : "Minimise"; },
      toggleTray: function () { this.minimised = !this.minimised; },
      dragOver: function () { this.dragging = true; },
      dragLeave: function () { this.dragging = false; },
      choose: function () { this.fileId = ""; this.$refs.input.click(); }, // a cancelled "new version" must not stick
      newVersion: function (e) { this.fileId = e.currentTarget.dataset.fileId; this.$refs.input.click(); },
      sameYes: function (e) { this.queue.answerSame(Number(e.currentTarget.closest("[data-same-row]").dataset.id), true); },
      sameNo: function (e) { this.queue.answerSame(Number(e.currentTarget.closest("[data-same-row]").dataset.id), false); },
      sendName: function (e) {
        var row = e.currentTarget.closest("[data-name-row]"), input = row.querySelector("input");
        this.queue.rename(Number(row.dataset.id), input.value);
      },
      cancel: function (e) { this.queue.cancel(Number(e.currentTarget.dataset.id)); },
      clearFinished: function () { this.queue.clearFinished(); },
    };
  });
});
