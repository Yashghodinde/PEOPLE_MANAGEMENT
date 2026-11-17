/* Upload queue and resume logic (M08.3). No browser globals are used directly: fetch, the hasher, the clock
   and storage are passed in, so the same code runs in the page and in the Node tests.

   Queue: at most `concurrency` files are in flight (default 2); the rest wait in order. Hashing is one at a time.
   Resume: every file gets an Idempotency-Key kept in storage under name+size+modified-time, so choosing the same
   file again after a reload finds the same server upload and continues from the server's received_bytes.
   Each piece is retried with growing waits; before retrying the client asks the server where it is (GET), so a
   piece whose answer was lost is never sent twice. 416 means "start from received_bytes". */
(function (root) {
  "use strict";

  var BACKOFF = [1000, 2000, 4000, 8000, 15000, 30000];
  var MAX_ATTEMPTS = 8;

  function errMessage(status, body) {
    if (body && body.message) return body.message;
    if (status === 401 || status === 403) return "Please sign in again.";
    if (status === 507) return "Server storage full. Ask the Super Admin to free some space.";
    return "Upload failed (" + status + ").";
  }

  function create(opts) {
    var concurrency = opts.concurrency || 2;
    var items = [], nextId = 1, running = 0, hashing = Promise.resolve();
    var sleep = opts.sleep || function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };

    function changed() { if (opts.onChange) opts.onChange(snapshot()); }
    function say(item, patch) { for (var k in patch) item[k] = patch[k]; changed(); }

    function snapshot() {
      return items.map(function (it) {
        var pct = it.size ? Math.min(100, Math.floor((it.sent / it.size) * 100)) : 0;
        if (it.state === "hashing") pct = Math.floor(it.hashed * 100);
        if (it.state === "done") pct = 100;
        var label = {
          queued: "Waiting", hashing: "Checking file", uploading: "Uploading", waiting: "Connection lost, retrying",
          done: "Done", failed: it.error || "Failed", cancelled: "Cancelled",
          checking: "Checking the name", needsname: "Needs a name", askversion: "Is this a new version?",
        }[it.state];
        return {
          id: it.id, name: it.displayName, size: it.size,
          needsName: it.state === "needsname", checking: it.state === "checking",
          askVersion: it.state === "askversion", sameText: it.same ? "Add as version " + it.same.next_version + " of " + it.same.name + "?" : "",
          message: it.nameMessage || "", suggestion: it.suggestion || "",
          prefill: it.suggestion || it.displayName, inputId: "rename-" + it.id,
          cancellable: it.state === "askversion" || it.state === "queued" || it.state === "hashing" || it.state === "uploading" || it.state === "waiting" || it.state === "needsname" || it.state === "checking", state: it.state, percent: pct, label: label,
          barStyle: "width:" + pct + "%", error: it.error || "", versionId: it.versionId || "",
          fileCode: it.fileCode || "",
          active: it.state === "queued" || it.state === "hashing" || it.state === "uploading" || it.state === "waiting",
          finished: it.state === "done" || it.state === "failed" || it.state === "cancelled",
          failed: it.state === "failed", ok: it.state === "done",
          showBar: it.state !== "failed" && it.state !== "cancelled" && it.state !== "needsname" && it.state !== "checking" && it.state !== "askversion",
          labelClass: it.state === "failed" || it.state === "needsname" ? "text-bad" : "text-ink-600",
        };
      });
    }

    function fingerprint(it) {
      return ["ots.up", it.taskId || "-", it.fileId || "new", it.relation, it.scope || "", it.workspaceId || "", it.displayName, it.size, it.modified].join("|");
    }
    function keyFor(it, fresh) {
      var fp = fingerprint(it), key = fresh ? null : opts.storage.get(fp);
      if (!key) { key = opts.uuid(); opts.storage.set(fp, key); }
      return key;
    }
    function forget(it) { opts.storage.remove(fingerprint(it)); }

    function request(it, method, url, init) {
      var ctl = new AbortController();
      it.abort = ctl;
      init = init || {};
      init.method = method;
      init.signal = ctl.signal;
      init.credentials = "same-origin";
      init.headers = Object.assign({ "X-CSRFToken": opts.csrf(), Accept: "application/json" }, init.headers || {});
      return opts.fetch(url, init).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) { return { status: r.status, body: body }; });
      });
    }

    function startBody(it, sha, key) {
      return JSON.stringify({
        idempotency_key: key, task_id: it.taskId || null, file_id: it.fileId || null, relation: it.relation, scope: it.scope || "", workspace_id: it.workspaceId || null, note: it.note || "",
        original_filename: it.name, display_name: it.displayName, size: it.size, sha256: sha,
      });
    }

    function hashFile(it) {
      var run = function () {
        if (it.cancelled) return Promise.reject({ cancelled: true });
        say(it, { state: "hashing", hashed: 0 });
        return opts.hashFile(it.file, function (p) { say(it, { hashed: p }); });
      };
      hashing = hashing.then(run, run);
      return hashing;
    }

    function begin(it, sha, restarts) {
      var key = keyFor(it, restarts > 0);
      return request(it, "POST", "/files/uploads", {
        body: startBody(it, sha, key), headers: { "Content-Type": "application/json" },
      }).then(function (r) {
        if (r.status !== 200 && r.status !== 201) {
          if (r.status === 409 && restarts < 1) { forget(it); return begin(it, sha, restarts + 1); }
          if (r.status === 422 && r.body && r.body.fields && r.body.fields.display_name) {
            throw { needsName: true, message: r.body.message, suggestion: r.body.suggestion || "" };
          }
          throw { fatal: errMessage(r.status, r.body) };
        }
        var b = r.body;
        if (b.version_id) return finish(it, b);
        if (b.state !== "RECEIVING") { // closed on the server (abandoned or rejected): start again with a new key
          if (restarts >= 1) throw { fatal: "This upload was closed by the server. Try again." };
          forget(it); return begin(it, sha, restarts + 1);
        }
        it.uploadId = b.upload_id; it.piece = b.piece_size;
        return pieces(it, b.received_bytes);
      });
    }

    function finish(it, b) {
      forget(it);
      say(it, { state: "done", sent: it.size, versionId: b.version_id, fileCode: b.file_code });
      if (opts.onDone) opts.onDone(it, b);
    }

    function resync(it) { // where does the server really stand?
      return request(it, "GET", "/files/uploads/" + it.uploadId).then(function (r) {
        if (r.status === 200) return r.body;
        if (r.status === 401 || r.status === 403 || r.status === 404) throw { fatal: errMessage(r.status, r.body) };
        throw new Error("server busy");
      });
    }

    function pieces(it, received) {
      var failures = 0, stuck = 0;
      function adopt(b) { // take the server's word for where the upload stands
        if (b.version_id) return finish(it, b);
        if (b.state !== "RECEIVING") { forget(it); throw { fatal: "The server closed this upload. Choose the file again." }; }
        received = b.received_bytes; return step();
      }
      function step() {
        if (it.cancelled) return Promise.reject({ cancelled: true });
        if (received >= it.size) return resync(it).then(adopt);
        var end = Math.min(received + it.piece, it.size) - 1;
        say(it, { state: "uploading", sent: received });
        return request(it, "PUT", "/files/uploads/" + it.uploadId, {
          body: it.file.slice(received, end + 1),
          headers: { "Content-Range": "bytes " + received + "-" + end + "/" + it.size, "Content-Type": "application/octet-stream" },
        }).then(function (r) {
          if (r.status === 200 || r.status === 201) {
            failures = 0; stuck = 0;
            if (r.body.version_id) return finish(it, r.body);
            received = r.body.received_bytes; say(it, { sent: received });
            return step();
          }
          if (r.status === 416) { // the server has a different offset: take it
            if (++stuck > 5) throw { fatal: "The server and this page cannot agree where the upload stands." };
            received = r.body.received_bytes; return step();
          }
          if (r.status === 408 || r.status === 429 || r.status >= 500) throw new Error("retry");
          if (r.body && r.body.error === "hash_mismatch") forget(it); // the next try must be a fresh upload
          throw { fatal: errMessage(r.status, r.body) };
        }).catch(retry);
      }
      function retry(err) {
        if (err && (err.fatal || err.cancelled)) throw err;
        if (it.cancelled) throw { cancelled: true };
        if (++failures > MAX_ATTEMPTS) throw { fatal: "The connection kept failing. Choose the file again to continue." };
        say(it, { state: "waiting" });
        return sleep(BACKOFF[Math.min(failures - 1, BACKOFF.length - 1)]).then(function () {
          return resync(it).then(adopt, retry);
        });
      }
      return step();
    }

    function run(it) {
      running++;
      return hashFile(it).then(function (sha) {
        if (it.cancelled) throw { cancelled: true };
        return begin(it, sha, 0);
      }).catch(function (err) {
        if (err && err.cancelled || it.cancelled) return say(it, { state: "cancelled" });
        // the server refused the name (a check was skipped or the rule changed): ask for a better one, keep the rest going
        if (err && err.needsName) return say(it, { state: "needsname", nameMessage: err.message, suggestion: err.suggestion, started: false });
        say(it, { state: "failed", error: err && err.fatal ? err.fatal : "Upload failed. Choose the file again to continue." });
      }).then(function () { running--; pump(); });
    }

    // Ask the server whether the name is acceptable. A failed check never blocks: the server judges again at start.
    function judgeName(it) {
      if (!opts.checkName) { say(it, { state: "queued", started: false }); return pump(); }
      say(it, { state: "checking" });
      return Promise.resolve(opts.checkName(it.displayName, it.taskId)).then(function (v) {
        if (it.cancelled) return;
        if (v && v.ok === false) say(it, { state: "needsname", nameMessage: v.message || "", suggestion: v.suggestion || "", started: false });
        else if (v && v.same && !it.fileId && !it.sameAnswered) say(it, { state: "askversion", same: v.same, nameMessage: "", started: false });
        else say(it, { state: "queued", nameMessage: "", started: false });
        pump();
      }, function () {
        if (it.cancelled) return;
        say(it, { state: "queued", started: false });
        pump();
      });
    }

    function pump() {
      for (var i = 0; i < items.length && running < concurrency; i++) {
        if (items[i].state === "queued" && !items[i].started) { items[i].started = true; run(items[i]); }
      }
    }

    return {
      add: function (file, meta) {
        var it = {
          id: nextId++, file: file, name: file.name, displayName: file.name, size: file.size, modified: file.lastModified || 0,
          taskId: meta.taskId, fileId: meta.fileId || "", relation: meta.relation || "SUBMISSION", scope: meta.scope || "", workspaceId: meta.workspaceId || "", note: meta.note || "",
          state: "queued", sent: 0, hashed: 0, started: false, cancelled: false,
        };
        if (!it.size) { it.state = "failed"; it.error = "This file is empty."; }
        items.push(it);
        if (it.size) judgeName(it); else changed();
        return it.id;
      },
      // "Add as version N of <name>?" Yes sends it as the next version of that document; No makes a separate one.
      // Never decided for the person.
      answerSame: function (id, yes) {
        var it = items.filter(function (x) { return x.id === id; })[0];
        if (!it || it.state !== "askversion") return;
        it.sameAnswered = true;
        if (yes) it.fileId = it.same.file_id;
        say(it, { state: "queued", started: false });
        pump();
      },
      // The person typed (or accepted) a better name for a file that was waiting for one; the other files carry on.
      rename: function (id, name) {
        var it = items.filter(function (x) { return x.id === id; })[0];
        name = (name || "").trim();
        if (!it || it.state !== "needsname" || !name) return;
        it.displayName = name;
        judgeName(it);
      },
      cancel: function (id) {
        var it = items.filter(function (x) { return x.id === id; })[0];
        if (!it || it.state === "done" || it.state === "failed" || it.state === "cancelled") return;
        it.cancelled = true;
        if (it.abort) it.abort.abort();
        if (it.state === "queued" || it.state === "needsname" || it.state === "checking") { it.state = "cancelled"; changed(); }
        if (it.uploadId) {
          request(it, "DELETE", "/files/uploads/" + it.uploadId).catch(function () {});
          forget(it);
        }
      },
      clearFinished: function () {
        items = items.filter(function (x) { return x.state === "queued" || x.state === "hashing" || x.state === "uploading" || x.state === "waiting" || x.state === "needsname" || x.state === "checking"; });
        changed();
      },
      snapshot: snapshot,
    };
  }

  var api = { create: create, BACKOFF: BACKOFF };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.OtsUploadCore = api;
})(typeof self !== "undefined" ? self : this);
