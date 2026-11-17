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

test("the board adapts to the screen: text scale, columns and rows follow its size and shape", () => {
  const full = Core.plan(1920, 1080, 10);
  assert.equal(full.s, 1); assert.equal(full.cols, 4); assert.equal(full.rows, 8);
  const hd = Core.plan(1280, 720, 10); // a 1080p TV at 150%: same picture, smaller pixels
  assert.ok(Math.abs(hd.s - 2 / 3) < 1e-9); assert.equal(hd.cols, 4); assert.equal(hd.rows, 8);
  const uhd = Core.plan(3840, 2160, 10); assert.equal(uhd.s, 2); assert.equal(uhd.cols, 4); assert.equal(uhd.rows, 8);
  const wide = Core.plan(2560, 1080, 10); assert.equal(wide.s, 1); assert.equal(wide.cols, 6); // uses the width
  const four3 = Core.plan(1024, 768, 10); assert.ok(four3.cols >= 3 && four3.rows >= 8); // uses the height
  const tall = Core.plan(1080, 1920, 10); assert.ok(tall.rows > full.rows); // portrait: more rows, not a tiny strip
});

test("cards that do not fit are counted, never cut in half", () => {
  assert.deepEqual([Core.plan(1920, 1080, 5).visible, Core.plan(1920, 1080, 5).hidden], [5, 0]);
  const p = Core.plan(1920, 1080, 40);
  assert.equal(p.capacity, 32); assert.equal(p.visible, 32); assert.equal(p.hidden, 8);
  const small = Core.plan(1024, 768, 60);
  assert.equal(small.visible + small.hidden, 60); assert.ok(small.visible === small.capacity);
});

test("a screen that cannot be measured falls back to full HD, and never to zero columns or rows", () => {
  for (const bad of [[0, 0, 5], [NaN, 100, 5], [undefined, undefined, 5], [-5, 10, 5]]) {
    const p = Core.plan(...bad); assert.equal(p.s, 1); assert.ok(p.cols >= 1 && p.rows >= 1);
  }
  const tiny = Core.plan(200, 120, 5); assert.ok(tiny.cols >= 1 && tiny.rows >= 1 && tiny.s > 0);
  assert.equal(Core.plan(1920, 1080, 0).visible, 0);
});
