// Node tests for the voice recorder's state machine and the live-hold counter (run by tests/files/test_js.py).
const test = require("node:test");
const assert = require("node:assert/strict");
const Voice = require("../../app/static/js/voice-core.js");
const Live = require("../../app/static/js/live-core.js");

// A fake microphone world: MediaRecorder, getUserMedia, clock, timers, and an upload we can make fail.
function world(opts = {}) {
  const w = { now: 1000, timers: [], posts: [], uploads: [], holds: 0, releases: 0, urls: [], stops: 0, sent: 0, snaps: [] };
  w.MediaRecorder = class {
    static isTypeSupported(t) { return (opts.types || ["audio/webm;codecs=opus"]).includes(t); }
    constructor(stream, o) { this.o = o; this.stream = stream; }
    start() { w.recorder = this; }
    stop() { if (opts.emptyRecording !== true) this.ondataavailable({ data: { size: 5000 } }); this.onstop(); }
  };
  w.mediaDevices = { getUserMedia: async () => { if (opts.denied) throw Object.assign(new Error("no"), { name: "NotAllowedError" }); return { getTracks: () => [{ stop: () => w.stops++ }] }; } };
  w.upload = async (file) => { w.uploads.push(file); if (opts.failUploads && w.uploads.length <= opts.failUploads) throw new Error("network down"); return { versionId: "v1", key: "k" + w.uploads.length }; };
  w.post = async (v, s, k) => { w.posts.push([v, s, k]); return {}; };
  w.core = Voice.create({
    maxSeconds: opts.max || 60, isSecure: opts.insecure ? false : true,
    mediaDevices: opts.noMic ? null : w.mediaDevices, MediaRecorder: opts.noRecorder ? null : w.MediaRecorder,
    Blob: class { constructor(parts, o) { this.size = opts.emptyRecording ? 0 : 5000; this.type = o.type; } },
    now: () => w.now,
    setTimeout: (f, ms) => (w.timers.push({ f, ms }), w.timers.length), clearTimeout: () => {},
    setInterval: () => 1, clearInterval: () => {},
    makeFile: (blob, name, type) => ({ blob, name, type }),
    createObjectURL: () => (w.urls.push("blob:x"), "blob:x"), revokeObjectURL: () => {},
    upload: w.upload, post: w.post, hold: () => w.holds++, release: () => w.releases++, onSent: () => w.sent++,
    onChange: (s) => w.snaps.push(s.state),
  });
  return w;
}

test("record, stop, preview, send: one upload, one post, then sent", async () => {
  const w = world();
  await w.core.start();
  assert.equal(w.core.state().state, "recording");
  w.now += 23_400;
  w.core.stop();
  const s = w.core.state();
  assert.equal(s.state, "preview"); assert.equal(s.seconds, 23); assert.equal(s.label, "0:23");
  assert.equal(w.core.blobUrl(), "blob:x");
  await w.core.send();
  assert.equal(w.core.state().state, "sent");
  assert.equal(w.uploads.length, 1); assert.equal(w.posts.length, 1);
  assert.deepEqual(w.posts[0].slice(0, 2), ["v1", 23]);
  assert.equal(w.uploads[0].name, "voice.webm"); assert.equal(w.sent, 1);
  assert.equal(w.holds, 1); assert.equal(w.releases, 1); // the page is held from record to sent, then let go
  assert.equal(w.stops, 1); // the microphone is switched off as soon as recording stops
});

test("pressing send twice sends once", async () => {
  const w = world();
  await w.core.start(); w.core.stop();
  await Promise.all([w.core.send(), w.core.send(), w.core.send()]);
  await w.core.send();
  assert.equal(w.uploads.length, 1); assert.equal(w.posts.length, 1);
});

test("a failed upload shows the failure, posts nothing, and retry sends the same file once", async () => {
  const w = world({ failUploads: 1 });
  await w.core.start(); w.now += 5000; w.core.stop();
  await w.core.send();
  assert.equal(w.core.state().state, "failed"); assert.match(w.core.state().error, /network down/);
  assert.equal(w.posts.length, 0); assert.equal(w.sent, 0); assert.equal(w.releases, 0); // still held: the recording is not lost
  await w.core.retry();
  assert.equal(w.core.state().state, "sent");
  assert.equal(w.posts.length, 1);
  assert.equal(w.uploads.length, 2); assert.strictEqual(w.uploads[0], w.uploads[1]); // the same File object both times
});

test("a failed post (the upload worked) can be retried and still posts once", async () => {
  const w = world();
  let calls = 0;
  const core = Voice.create({ ...{}, maxSeconds: 60, isSecure: true, mediaDevices: w.mediaDevices, MediaRecorder: w.MediaRecorder,
    Blob: class { constructor() { this.size = 10; } }, now: () => w.now, setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {},
    makeFile: (b, n, t) => ({ n }), upload: async () => ({ versionId: "v9", key: "k" }), post: async () => { if (++calls === 1) throw new Error("server busy"); return {}; },
    hold() {}, release() {} });
  await core.start(); core.stop(); await core.send();
  assert.equal(core.state().state, "failed");
  await core.retry();
  assert.equal(core.state().state, "sent"); assert.equal(calls, 2);
});

test("discard throws the recording away and lets the page refresh again", async () => {
  const w = world();
  await w.core.start(); w.core.stop();
  w.core.discard();
  assert.equal(w.core.state().state, "ready"); assert.equal(w.core.blobUrl(), "");
  assert.equal(w.releases, 1);
  await w.core.send(); // nothing to send now
  assert.equal(w.uploads.length, 0);
});

test("discarding while recording stops the microphone and sends nothing", async () => {
  const w = world();
  await w.core.start();
  w.core.discard();
  assert.equal(w.core.state().state, "ready"); assert.equal(w.stops, 1); assert.equal(w.uploads.length, 0);
});

test("a recording stops by itself at the limit", async () => {
  const w = world({ max: 30 });
  await w.core.start();
  const t = w.timers.find((x) => x.ms === 30_000);
  assert.ok(t, "an auto-stop timer at the limit");
  w.now += 30_000; t.f();
  assert.equal(w.core.state().state, "preview"); assert.equal(w.core.state().seconds, 30);
});

test("nothing recorded goes back to ready with a message", async () => {
  const w = world({ emptyRecording: true });
  await w.core.start(); w.core.stop();
  assert.equal(w.core.state().state, "ready"); assert.match(w.core.state().message, /Nothing was recorded/);
});

test("clear messages for a blocked microphone, no microphone, no recorder, an insecure page", async () => {
  let w = world({ denied: true }); await w.core.start();
  assert.equal(w.core.state().state, "denied"); assert.match(w.core.state().message, /blocked/);
  w = world({ noMic: true }); await w.core.start();
  assert.equal(w.core.state().state, "unsupported"); assert.match(w.core.state().message, /cannot record/);
  w = world({ noRecorder: true }); await w.core.start();
  assert.match(w.core.state().message, /type your message/);
  w = world({ insecure: true }); await w.core.start();
  assert.match(w.core.state().message, /secure page \(https\) or localhost/);
  w = world({ types: [] }); await w.core.start();
  assert.match(w.core.state().message, /no audio format/);
});

test("the recorder prefers webm, then mp4 (Safari), then ogg, and names the file by type", () => {
  const fake = (ok) => ({ isTypeSupported: (t) => ok.includes(t) });
  assert.equal(Voice.pickType(fake(["audio/webm;codecs=opus", "audio/mp4"])), "audio/webm;codecs=opus");
  assert.equal(Voice.pickType(fake(["audio/mp4"])), "audio/mp4");
  assert.equal(Voice.pickType(fake(["audio/ogg;codecs=opus"])), "audio/ogg;codecs=opus");
  assert.equal(Voice.pickType(fake([])), "");
  assert.deepEqual(["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"].map(Voice.extensionFor), ["webm", "m4a", "ogg"]);
});

test("clock formats mm:ss", () => {
  assert.deepEqual([0, 5, 59, 60, 83, 300].map(Voice.clock), ["0:00", "0:05", "0:59", "1:00", "1:23", "5:00"]);
});

// --- the hold counter: uploads, recording, preview and playback all hold the page still ---------------------------

test("the page stays held until every holder lets go, then the waiting refresh runs", () => {
  const h = Live.holdCounter();
  let refreshed = 0;
  h.onRelease = () => refreshed++;
  assert.equal(h.held(), false);
  const upload = h.acquire("uploading"), rec = h.acquire("recording");
  assert.equal(h.held(), true); assert.deepEqual(h.reasons().sort(), ["recording", "uploading"]);
  h.release(upload);
  assert.equal(h.held(), true); assert.equal(refreshed, 0); // the recording still holds it
  h.release(rec);
  assert.equal(h.held(), false); assert.equal(refreshed, 1);
  h.release(rec); h.release(999); // releasing twice, or a token never given, changes nothing
  assert.equal(refreshed, 1); assert.equal(h.held(), false);
});
