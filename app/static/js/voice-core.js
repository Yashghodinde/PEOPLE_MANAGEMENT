/* Voice recorder logic (IMP-10). No browser globals used directly (the microphone, MediaRecorder, the clock and the
   upload are passed in), so the same code runs in the page and in Node tests.

   States: ready -> recording -> preview -> uploading -> sent. A failed upload goes to "failed" with Retry and
   Discard. The recording stays in memory only. It is sent once: sending twice does nothing, and a retry offers the
   same file with the same key, so the server finishes the same upload and writes exactly one message. */
(function (root) {
  "use strict";

  var PREFERRED = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"];

  function pickType(MR) {
    if (!MR) return "";
    if (!MR.isTypeSupported) return PREFERRED[0];
    for (var i = 0; i < PREFERRED.length; i++) if (MR.isTypeSupported(PREFERRED[i])) return PREFERRED[i];
    return "";
  }
  function extensionFor(type) {
    if (/mp4/.test(type)) return "m4a";
    if (/ogg/.test(type)) return "ogg";
    return "webm";
  }
  function clock(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    return Math.floor(seconds / 60) + ":" + ("0" + (seconds % 60)).slice(-2);
  }
  function why(err) {
    var n = err && err.name;
    if (n === "NotAllowedError" || n === "SecurityError") return "The microphone is blocked. Allow it for this site in the browser, then try again.";
    if (n === "NotFoundError" || n === "OverconstrainedError") return "No microphone was found on this computer.";
    if (n === "NotReadableError") return "The microphone is being used by another program.";
    return "The recording could not start.";
  }

  function create(o) {
    var maxSeconds = o.maxSeconds || 300;
    var s = {
      state: "ready", message: "", seconds: 0, blob: null, type: "", uploadKey: null, versionId: "", error: "",
    };
    var stream = null, rec = null, chunks = [], startedAt = 0, timer = null, tick = null, sending = false;
    var file = null, url = "";

    function say(patch) { for (var k in patch) s[k] = patch[k]; if (o.onChange) o.onChange(snap()); }
    function snap() { return JSON.parse(JSON.stringify({ state: s.state, message: s.message, seconds: s.seconds, error: s.error, label: clock(s.seconds), type: s.type })); }
    function freeUrl() { if (url && o.revokeObjectURL) o.revokeObjectURL(url); url = ""; }
    function stopTracks() { if (stream) { stream.getTracks().forEach(function (t) { t.stop(); }); stream = null; } }
    function clearTimers() { if (timer) { o.clearTimeout(timer); timer = null; } if (tick) { o.clearInterval(tick); tick = null; } }

    function supported() {
      if (!o.isSecure) return "Recording needs a secure page (https) or localhost. You can still type your message.";
      if (!o.mediaDevices || !o.mediaDevices.getUserMedia || !o.MediaRecorder) return "This browser cannot record audio. You can still type your message.";
      if (!pickType(o.MediaRecorder)) return "This browser has no audio format it can record in. You can still type your message.";
      return "";
    }

    function start() {
      if (s.state === "recording" || s.state === "uploading") return Promise.resolve();
      var problem = supported();
      if (problem) { say({ state: "unsupported", message: problem }); return Promise.resolve(); }
      return o.mediaDevices.getUserMedia({ audio: true }).then(function (st) {
        freeUrl(); stream = st; chunks = []; file = null; s.uploadKey = null; s.blob = null;
        s.type = pickType(o.MediaRecorder);
        rec = new o.MediaRecorder(st, s.type ? { mimeType: s.type } : undefined);
        rec.ondataavailable = function (e) { if (e && e.data && e.data.size) chunks.push(e.data); };
        rec.onstop = finishRecording;
        rec.start();
        startedAt = o.now();
        timer = o.setTimeout(stop, maxSeconds * 1000); // never longer than the limit
        tick = o.setInterval(function () { say({ seconds: Math.min(maxSeconds, (o.now() - startedAt) / 1000) }); }, 250);
        say({ state: "recording", message: "", seconds: 0, error: "" });
        if (o.hold) o.hold("recording");
      }, function (err) {
        stopTracks();
        say({ state: err && err.name === "NotAllowedError" ? "denied" : "unsupported", message: why(err) });
      });
    }

    function stop() {
      if (s.state !== "recording" || !rec) return;
      clearTimers();
      s.seconds = Math.max(1, Math.min(maxSeconds, Math.round((o.now() - startedAt) / 1000)));
      try { rec.stop(); } catch (e) { finishRecording(); }
    }

    function finishRecording() {
      stopTracks();
      var blob = new o.Blob(chunks, { type: s.type || "audio/webm" });
      chunks = [];
      if (!blob.size) { say({ state: "ready", message: "Nothing was recorded. Try again." }); if (o.release) o.release(); return; }
      s.blob = blob;
      say({ state: "preview", message: "" });
    }

    function discard() {
      if (s.state === "uploading") return; // an upload in flight is cancelled by the page, not silently dropped
      clearTimers(); stopTracks();
      if (rec && s.state === "recording") { rec.onstop = null; try { rec.stop(); } catch (e) {} }
      freeUrl(); file = null; chunks = []; s.blob = null; s.uploadKey = null; sending = false;
      say({ state: "ready", message: "", seconds: 0, error: "", versionId: "" });
      if (o.release) o.release();
    }

    function send() {
      if (sending || s.state === "sent" || s.state === "uploading" || !s.blob) return Promise.resolve(); // once only
      sending = true;
      if (!file) file = o.makeFile(s.blob, "voice." + extensionFor(s.type), s.type);
      say({ state: "uploading", error: "" });
      return Promise.resolve(o.upload(file)).then(function (done) {
        s.versionId = done.versionId;
        return o.post(done.versionId, s.seconds, done.key);
      }).then(function () {
        freeUrl(); s.blob = null; file = null;
        say({ state: "sent", message: "Sent." });
        if (o.release) o.release();
        if (o.onSent) o.onSent();
      }, function (err) {
        sending = false;
        say({ state: "failed", error: (err && err.message) || "It did not send. Try again; nothing was posted." });
      });
    }

    function retry() {
      if (s.state !== "failed") return Promise.resolve();
      s.state = "preview"; // same file, same upload key: the server finishes the same upload, one message
      return send();
    }

    function blobUrl() {
      if (!s.blob || !o.createObjectURL) return "";
      if (!url) url = o.createObjectURL(s.blob);
      return url;
    }

    return { blobUrl: blobUrl, start: start, stop: stop, discard: discard, send: send, retry: retry, state: function () { return snap(); }, supported: supported };
  }

  var api = { create: create, pickType: pickType, extensionFor: extensionFor, clock: clock, why: why };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.OtsVoiceCore = api;
})(typeof self !== "undefined" ? self : this);
