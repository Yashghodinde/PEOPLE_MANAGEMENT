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

  window.OtsUploadEnv = { csrf: csrf, storage: storage, uuid: uuid, hashFile: hashFile }; // voice.js reuses these

  Alpine.data("uploader", function () {
    return {
      items: [], relation: "SUBMISSION", fileId: "", note: "", dragging: false, queue: null,
      init: function () {
        var self = this, d = this.$el.dataset;
        this.taskId = d.taskId; this.scope = d.scope || ""; // scope "personal" = into My storage, no task
        this.relation = d.defaultRelation || "SUBMISSION";
        this.queue = OtsUploadCore.create({
          fetch: window.fetch.bind(window), hashFile: hashFile, uuid: uuid, storage: storage(), csrf: csrf,
          concurrency: 2, checkName: checkName,
          onChange: function (list) {
            self.items = list;
            // the page must not swap itself under a running upload (live.js looks at the hold counter)
            var busy = list.some(function (i) { return i.active; });
            if (busy && !self.hold && window.OtsLiveHold) self.hold = window.OtsLiveHold.acquire("uploading");
            else if (!busy && self.hold && window.OtsLiveHold) { window.OtsLiveHold.release(self.hold); self.hold = 0; }
          },
          onDone: function () { document.body.dispatchEvent(new CustomEvent("ots:file-uploaded")); },
        });
      },
      get dropClass() { return this.dragging ? "border-accent bg-line" : "border-line"; },
      get hasItems() { return this.items.length > 0; },
      get anyFinished() { return this.items.some(function (i) { return i.finished; }); },
      take: function (fileList) {
        var meta = { taskId: this.taskId, relation: this.relation, fileId: this.fileId, scope: this.scope, note: this.note };
        for (var i = 0; i < fileList.length; i++) this.queue.add(fileList[i], meta);
        this.fileId = ""; this.note = ""; this.relation = this.$el.dataset.defaultRelation || "SUBMISSION";
      },
      picked: function (e) { this.take(e.target.files); e.target.value = ""; },
      dropped: function (e) {
        this.dragging = false;
        this.fileId = ""; // dropped files are new files, even if "Upload a new version" was clicked and then cancelled
        if (e.dataTransfer) this.take(e.dataTransfer.files);
      },
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
