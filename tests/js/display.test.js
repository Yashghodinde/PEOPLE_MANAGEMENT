// Node tests for the TV board's connection rules (run by tests/live/test_js.py), with a fake clock.
const test = require("node:test");
const assert = require("node:assert/strict");
const Core = require("../../app/static/js/display-core.js");

function rig() {
  let t = 0, id = 0; const q = new Map(); const log = []; let handlers = null;
  const o = {
    now: () => t,
    setTimeout: (f, ms) => { q.set(++id, [t + ms, f]); return id; },
    clearTimeout: (i) => q.delete(i),
    fetchBoard: () => log.push("fetch"),
    reload: () => log.push("reload"),
    closeStream: () => log.push("close"),
    openStream: (h) => { handlers = h; log.push("open"); },
  };
  const advance = (ms) => {
    const end = t + ms;
    for (;;) {
      const next = [...q.entries()].filter(([, [at]]) => at <= end).sort((a, b) => a[1][0] - b[1][0])[0];
      if (!next) break;
      q.delete(next[0]); t = Math.max(t, next[1][0]); next[1][1]();
    }
    t = end;
  };
  const core = Core.create(o);
  return { core, log, advance, send: (type) => handlers.event(type), fail: () => handlers.error(), count: (x) => log.filter((l) => l === x).length };
}

test("a board change fetches the board; a fresh connection starts from the current board", () => {
  const r = rig(); r.core.start();
  r.send("hello"); assert.equal(r.count("fetch"), 1);
  r.send("board-changed"); assert.equal(r.count("fetch"), 2);
});

test("a reload message reloads the page", () => {
  const r = rig(); r.core.start(); r.send("reload");
  assert.equal(r.count("reload"), 1);
});

test("silence for 60 s reloads; pings keep it alive", () => {
  const r = rig(); r.core.start(); r.send("hello");
  for (let i = 0; i < 6; i++) { r.advance(25000); r.send("ping"); } // 150 s of pings
  assert.equal(r.count("reload"), 0);
  r.advance(61000);
  assert.equal(r.count("reload"), 1);
});

test("three failed connections switch to polling every 30 s", () => {
  const r = rig(); r.core.start();
  r.fail(); r.fail(); assert.equal(r.core.mode(), "live");
  r.fail(); assert.equal(r.core.mode(), "poll");
  assert.equal(r.count("fetch"), 1); // the first poll is immediate
  r.advance(30000); assert.equal(r.count("fetch"), 2);
  r.advance(60000); assert.equal(r.count("fetch"), 4);
  assert.equal(r.count("reload"), 0); // silence is expected while polling
});

test("polling mode retries the live stream every 5 minutes, and stops polling once it works", () => {
  const r = rig(); r.core.start(); r.fail(); r.fail(); r.fail();
  r.advance(300000);
  assert.equal(r.count("close"), 1); assert.equal(r.count("open"), 2);
  r.send("hello");
  assert.equal(r.core.mode(), "live");
  const fetched = r.count("fetch");
  r.advance(120000);
  assert.equal(r.count("fetch"), fetched); // no more polling
});

test("a failed retry goes back to polling and tries again 5 minutes later", () => {
  const r = rig(); r.core.start(); r.fail(); r.fail(); r.fail();
  r.advance(300000); // retry opens a new stream
  r.fail(); r.fail(); r.fail();
  assert.equal(r.core.mode(), "poll");
  r.advance(300000);
  assert.equal(r.count("open"), 3);
});

test("a hello resets the failure count", () => {
  const r = rig(); r.core.start(); r.fail(); r.fail(); r.send("hello"); r.fail(); r.fail();
  assert.equal(r.core.mode(), "live");
});
