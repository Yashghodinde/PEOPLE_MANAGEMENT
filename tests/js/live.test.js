// Node tests for the live page-refresh rules (run by tests/live/test_js.py).
const test = require("node:test");
const assert = require("node:assert/strict");
const Core = require("../../app/static/js/live-core.js");

const ID = "01a1cfa6-3666-73d6-9c6f-b46e42f647bf";
const OTHER = "01a1cfa6-3672-7574-bee4-0261217b60d8";

test("page kinds", () => {
  assert.deepEqual(Core.pageKind(`/app/tasks/${ID}`), { kind: "detail", id: ID });
  assert.equal(Core.pageKind("/app/tasks").kind, "landing");
  assert.equal(Core.pageKind("/app/tasks/all").kind, "list");
  assert.equal(Core.pageKind(`/app/tasks/by/${ID}`).kind, "group");
  assert.equal(Core.pageKind(`/app/tasks/to/${ID}`).kind, "group");
  assert.equal(Core.pageKind("/app/dashboard").kind, "dashboard");
  assert.equal(Core.pageKind("/app/notifications").kind, "notifications");
  for (const p of ["/app/tasks/new", `/app/tasks/${ID}/edit`, "/app/people/new", "/app/attendance", "/files/library"]) {
    assert.equal(Core.pageKind(p).kind, "other", p);
  }
});

test("a task change refreshes only the page that shows that task, the list and the dashboard", () => {
  const m = { type: "task-changed", task_id: ID };
  assert.equal(Core.wants(m, `/app/tasks/${ID}`).page, true);
  assert.equal(Core.wants(m, `/app/tasks/${OTHER}`).page, false);
  for (const p of ["/app/tasks", "/app/tasks/all", `/app/tasks/by/${ID}`, `/app/tasks/to/${OTHER}`, "/app/dashboard"]) {
    assert.equal(Core.wants(m, p).page, true, p);
  }
  assert.equal(Core.wants(m, "/app/tasks/new").page, false); // never under a form
  assert.equal(Core.wants({ type: "task-changed", task_id: ID.toUpperCase() }, `/app/tasks/${ID}`).page, true);
});

test("notifications, files, alerts and reload", () => {
  assert.deepEqual(Core.wants({ type: "notification" }, "/app/tasks/all"), { page: false, bell: true, files: false, alert: false });
  for (const p of ["/app/tasks", `/app/tasks/by/${ID}`, `/app/tasks/to/${ID}`]) {
    assert.deepEqual(Core.wants({ type: "notification" }, p), { page: true, bell: true, files: false, alert: false }, p); // unread marks change
  }
  assert.equal(Core.wants({ type: "notification" }, "/app/notifications").page, true);
  assert.equal(Core.wants({ type: "file-archived" }, "/app/tasks").files, true);
  assert.equal(Core.wants({ type: "alert" }, "/app/tasks").alert, true);
  const r = Core.wants({ type: "reload" }, `/app/tasks/${ID}`);
  assert.equal(r.page && r.bell && r.files, true);
  assert.equal(Core.wants({ type: "reload" }, "/app/tasks/new").page, false);
  assert.equal(Core.wants({ type: "board-changed" }, "/app/tasks").page, false);
  assert.equal(Core.wants(null, "/app/tasks").page, false);
});

function fakeTimers() {
  let t = 0, id = 0; const q = new Map();
  return {
    now: () => t, setTimeout: (f, ms) => { q.set(++id, [t + ms, f]); return id; },
    advance(ms) {
      const end = t + ms;
      for (;;) {
        const next = [...q.entries()].filter(([, [at]]) => at <= end).sort((a, b) => a[1][0] - b[1][0])[0];
        if (!next) break;
        q.delete(next[0]); t = Math.max(t, next[1][0]); next[1][1]();
      }
      t = end;
    },
  };
}
const flush = () => new Promise((r) => setImmediate(r));

test("a burst of messages is one refresh", async () => {
  const tm = fakeTimers(); let n = 0;
  const s = Core.scheduler({ refresh: async () => { n++; }, isSafe: () => true, now: tm.now, setTimeout: tm.setTimeout });
  for (let i = 0; i < 5; i++) s.request();
  tm.advance(399); await flush(); assert.equal(n, 0);
  tm.advance(2); await flush(); assert.equal(n, 1);
});

test("while the person is busy it waits, tells them once after 12 s, and refreshes when they finish", async () => {
  const tm = fakeTimers(); let n = 0, told = 0, safe = false;
  const s = Core.scheduler({ refresh: async () => { n++; }, isSafe: () => safe, now: tm.now, setTimeout: tm.setTimeout, onStale: () => told++ });
  s.request();
  tm.advance(5000); await flush(); assert.equal(n, 0); assert.equal(told, 0);
  tm.advance(10000); await flush(); assert.equal(n, 0); assert.equal(told, 1);
  tm.advance(10000); await flush(); assert.equal(told, 1); // only once
  safe = true; tm.advance(2000); await flush(); assert.equal(n, 1);
});

test("a message that arrives during a refresh causes one more afterwards", async () => {
  const tm = fakeTimers(); let n = 0, release;
  const s = Core.scheduler({
    refresh: () => { n++; return new Promise((r) => { release = r; }); },
    isSafe: () => true, now: tm.now, setTimeout: tm.setTimeout,
  });
  s.request(); tm.advance(500); await flush(); assert.equal(n, 1);
  s.request(); tm.advance(500); await flush(); assert.equal(n, 1); // still running
  release(); await flush(); tm.advance(500); await flush(); assert.equal(n, 2);
});

test("a failing refresh does not stop later ones", async () => {
  const tm = fakeTimers(); let n = 0;
  const s = Core.scheduler({ refresh: async () => { n++; throw new Error("offline"); }, isSafe: () => true, now: tm.now, setTimeout: tm.setTimeout });
  s.request(); tm.advance(500); await flush();
  s.request(); tm.advance(500); await flush();
  assert.equal(n, 2);
});
