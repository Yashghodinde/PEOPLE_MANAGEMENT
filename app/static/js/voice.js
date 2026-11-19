/* Alpine components for voice messages (IMP-10): the recorder in the discussion composer and the player.
   CSP build: no inline expressions, so everything the templates read is a property or method here. The state
   machine is in voice-core.js; the upload goes through the same resumable queue as every other file. */
document.addEventListener("alpine:init", function () {
  "use strict";

  function holdApi() { return window.OtsLiveHold || { acquire: function () { return 0; }, release: function () {} }; }

  // Send the recording through the normal upload queue, relation VOICE. Resolves when the server has it.
  function uploadRecording(file, taskId) {
    var env = window.OtsUploadEnv;
    return new Promise(function (resolve, reject) {
      var done = false;
      var q = OtsUploadCore.create({
        fetch: window.fetch.bind(window), hashFile: env.hashFile, uuid: env.uuid, storage: env.storage(), csrf: env.csrf,
        concurrency: 1,
        onChange: function (list) {
          var it = list[0];
          if (!it || done) return;
          if (it.failed) { done = true; reject(new Error(it.error || "The upload failed.")); }
          else if (it.state === "cancelled") { done = true; reject(new Error("The upload was cancelled.")); }
        },
        onDone: function (it, b) { done = true; resolve({ versionId: b.version_id, key: env.uuid() }); },
      });
      q.add(file, { taskId: taskId, relation: "VOICE" });
    });
  }

  function postMessage(taskId, versionId, seconds, key) {
    var env = window.OtsUploadEnv;
    return fetch("/app/tasks/" + taskId + "/voice", {
      method: "POST", credentials: "same-origin",
      headers: { "X-CSRFToken": env.csrf(), "Idempotency-Key": key, Accept: "application/json",
                 "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ version_id: versionId, seconds: String(Math.max(1, Math.round(seconds))) }).toString(),
    }).then(function (r) {
      if (r.ok) return r.json();
      return r.json().catch(function () { return {}; }).then(function (b) { throw new Error(b.message || "The message was not posted (" + r.status + ")."); });
    });
  }

  Alpine.data("voiceRecorder", function () {
    return {
      st: { state: "ready", message: "", seconds: 0, error: "", label: "0:00" }, previewUrl: "", core: null, hold: 0,
      init: function () {
        var self = this, d = this.$el.dataset, taskId = d.taskId;
        var max = Number(d.maxSeconds) || 300;
        this.maxLabel = OtsVoiceCore.clock(max);
        this.core = OtsVoiceCore.create({
          maxSeconds: max, isSecure: window.isSecureContext !== false,
          mediaDevices: navigator.mediaDevices, MediaRecorder: window.MediaRecorder, Blob: window.Blob,
          now: Date.now, setTimeout: window.setTimeout.bind(window), clearTimeout: window.clearTimeout.bind(window),
          setInterval: window.setInterval.bind(window), clearInterval: window.clearInterval.bind(window),
          createObjectURL: window.URL.createObjectURL.bind(window.URL),
          revokeObjectURL: window.URL.revokeObjectURL.bind(window.URL),
          makeFile: function (blob, name, type) { return new File([blob], name, { type: type, lastModified: Date.now() }); },
          upload: function (file) { return uploadRecording(file, taskId); },
          post: function (versionId, seconds, key) { return postMessage(taskId, versionId, seconds, key); },
          hold: function (why) { if (!self.hold) self.hold = holdApi().acquire(why); },
          release: function () { if (self.hold) { holdApi().release(self.hold); self.hold = 0; } },
          onSent: function () { document.dispatchEvent(new CustomEvent("ots:comment-added")); },
          onChange: function (snap) {
            self.st = snap;
            if (snap.state === "preview" && !self.previewUrl && self.core) self.previewUrl = self.core.blobUrl();
            if (snap.state === "ready" || snap.state === "sent") self.previewUrl = "";
          },
        });
        var problem = this.core.supported();
        if (problem) this.st = { state: "unsupported", message: problem, seconds: 0, error: "", label: "0:00" };
        window.addEventListener("beforeunload", function (e) {
          if (self.dirty) { e.preventDefault(); e.returnValue = ""; }
        });
      },
      get dirty() { return ["recording", "preview", "uploading", "failed"].indexOf(this.st.state) !== -1; },
      get idleVoice() { return !(this.st.state === "recording" || this.st.state === "preview" || this.st.state === "uploading" || this.st.state === "failed"); },
      get isReady() { return this.st.state === "ready" || this.st.state === "sent"; },
      get isRecording() { return this.st.state === "recording"; },
      get isPreview() { return this.st.state === "preview"; },
      get isUploading() { return this.st.state === "uploading"; },
      get isFailed() { return this.st.state === "failed"; },
      get isSent() { return this.st.state === "sent"; },
      get isBlocked() { return this.st.state === "unsupported" || this.st.state === "denied"; },
      get canDiscard() { return this.st.state === "preview" || this.st.state === "failed"; },
      get timerText() { return this.st.label + " / " + this.maxLabel; },
      get note() { return this.st.error || this.st.message; },
      get noteClass() { return this.isBlocked || this.isFailed ? "text-bad" : "text-ink-600"; },
      start: function () { this.previewUrl = ""; this.core.start(); },
      stop: function () { this.core.stop(); },
      send: function () { this.core.send(); },
      retry: function () { this.core.retry(); },
      discard: function () { this.core.discard(); this.previewUrl = ""; },
    };
  });

  Alpine.data("voicePlayer", function () {
    return {
      playing: false, now: 0, total: 0, failed: false, hold: 0,
      init: function () { this.root = this.$el; this.src = this.$el.dataset.src; this.total = Number(this.$el.dataset.seconds) || 0; },
      get audio() { return this.$refs.audio; },
      get label() { return OtsVoiceCore.clock(this.now) + " / " + OtsVoiceCore.clock(this.total); },
      get barStyle() { return "width:" + (this.total ? Math.min(100, (this.now / this.total) * 100) : 0) + "%"; },
      get buttonLabel() { return this.playing ? "Pause voice message" : "Play voice message"; },
      toggle: function () {
        var a = this.audio, self = this;
        if (!a.getAttribute("src")) a.setAttribute("src", this.src); // loaded only when asked: preload none
        if (a.paused) {
          var p = a.play();
          if (p && p.catch) p.catch(function () { self.failed = true; });
        } else a.pause();
      },
      onPlay: function () { this.failed = false; this.playing = true; if (!this.hold) this.hold = holdApi().acquire("playing"); },
      onPause: function () { this.playing = false; this.letGo(); },
      onEnd: function () { this.playing = false; this.now = 0; this.letGo(); },
      onTime: function () { this.now = this.audio.currentTime; if (this.audio.duration && isFinite(this.audio.duration)) this.total = Math.max(this.total, this.audio.duration); },
      onError: function () { this.failed = true; this.playing = false; this.letGo(); },
      letGo: function () { if (this.hold) { holdApi().release(this.hold); this.hold = 0; } },
      seek: function (e) {
        var bar = e.currentTarget, a = this.audio;
        if (!a.getAttribute("src")) a.setAttribute("src", this.src);
        var r = bar.getBoundingClientRect();
        var frac = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
        var t = (a.duration && isFinite(a.duration) ? a.duration : this.total) * frac;
        a.currentTime = t; this.now = t;
      },
    };
  });
});
