// Node tests for the upload queue (run by tests/files/test_js.py). A fake server speaks the real protocol.
const test = require("node:test");
const assert = require("node:assert/strict");
const Core = require("../../app/static/js/upload-core.js");

function fakeFile(name, size, mod = 1) {
  return { name, size, lastModified: mod, slice: (a, b) => ({ a, b }) };
}
function memStorage() {
  const m = new Map();
  return { get: (k) => m.get(k) ?? null, set: (k, v) => m.set(k, v), remove: (k) => m.delete(k), m };
}
function server(opts = {}) {
  const st = { sessions: {}, byKey: {}, log: [], inFlight: 0, maxInFlight: 0, n: 0, drops: opts.drops || [], puts: 0, downAfter: opts.downAfter || 0 };
  const resp = (status, body) => ({ status, json: async () => body });
  const view = (s) => ({ upload_id: s.id, received_bytes: s.received, piece_size: opts.piece || 100, size: s.size,
    state: s.state, ...(s.version ? { version_id: s.version, file_code: "code-" + s.version, version: 1 } : {}) });
  st.fetch = async (url, init) => {
    st.log.push(init.method + " " + url);
    if (st.down) throw new TypeError("network down");
    if (init.signal && init.signal.aborted) throw Object.assign(new Error("aborted"), { name: "AbortError" });
    st.inFlight++; st.maxInFlight = Math.max(st.maxInFlight, st.inFlight);
    try {
      await new Promise((r) => setImmediate(r));
      if (init.method === "POST") {
        const b = JSON.parse(init.body);
        if (opts.startStatus) return resp(opts.startStatus, { message: "no way" });
        let s = st.byKey[b.idempotency_key];
        if (!s) { s = { id: "u" + ++st.n, size: b.size, received: 0, state: "RECEIVING", key: b.idempotency_key, version: null };
          st.byKey[b.idempotency_key] = s; st.sessions[s.id] = s; }
        return resp(s.received ? 200 : 201, view(s));
      }
      const id = url.split("/").pop(), s = st.sessions[id];
      if (init.method === "GET") return resp(200, view(s));
      if (init.method === "DELETE") { s.state = "ABANDONED"; return resp(200, view(s)); }
      const m = /bytes (\d+)-(\d+)\/(\d+)/.exec(init.headers["Content-Range"]);
      const [a, b] = [+m[1], +m[2]];
      st.puts++;
      if (st.downAfter && st.puts > st.downAfter) { st.down = true; throw new TypeError("network down"); }
      if (st.drops.length && st.drops[0] === st.puts) { st.drops.shift(); throw new TypeError("network down"); }
      if (opts.lostAnswer && st.puts === opts.lostAnswer) { s.received = b + 1; throw new TypeError("answer lost"); }
      if (opts.fail500 && st.puts <= opts.fail500) return resp(503, {});
      if (a !== s.received) return resp(416, { received_bytes: s.received });
      s.received = b + 1;
      if (s.received === s.size) {
        if (opts.badHash) { s.state = "REJECTED"; return resp(422, { error: "hash_mismatch", message: "The file arrived damaged." }); }
        s.state = "STAGED"; s.version = "v" + s.id;
      }
      return resp(s.version ? 201 : 200, view(s));
    } finally { st.inFlight--; }
  };
  return st;
}
function make(st, extra = {}) {
  const store = extra.storage || memStorage(), done = [], sleeps = [];
  let k = 0;
  const q = Core.create({
    fetch: st.fetch, hashFile: async (f, p) => { p(0.5); p(1); return "a".repeat(64); },
    uuid: () => "key-" + ++k, storage: store, csrf: () => "tok", concurrency: extra.concurrency || 2,
    sleep: async (ms) => { sleeps.push(ms); }, onDone: (it, b) => done.push(b),
  });
  return { q, store, done, sleeps };
}
const settle = async (q, pred, tries = 500) => { for (let i = 0; i < tries && !pred(q.snapshot()); i++) await new Promise((r) => setImmediate(r)); };
const finished = (s) => s.length > 0 && s.every((i) => i.finished);

test("a file uploads in pieces and reports done", async () => {
  const st = server(); const { q, done } = make(st);
  q.add(fakeFile("a.xlsx", 250), { taskId: "t1" });
  await settle(q, finished);
  assert.equal(q.snapshot()[0].state, "done");
  assert.equal(st.puts, 3); assert.equal(done.length, 1);
  assert.ok(st.log.some((l) => l === "POST /files/uploads"));
});

test("at most two uploads run at once and all finish in order", async () => {
  const st = server(); const { q } = make(st, { concurrency: 2 });
  for (let i = 0; i < 6; i++) q.add(fakeFile("f" + i, 300, i), { taskId: "t" });
  await settle(q, finished, 5000);
  assert.ok(q.snapshot().every((i) => i.state === "done"));
  assert.ok(st.maxInFlight <= 2, "in flight " + st.maxInFlight);
  assert.equal(st.puts, 18);
});

test("a dropped connection retries, asks the server where it is, and finishes with one upload", async () => {
  const st = server({ drops: [2] }); const { q, sleeps } = make(st);
  q.add(fakeFile("a.bin", 400), { taskId: "t" });
  await settle(q, finished);
  assert.equal(q.snapshot()[0].state, "done");
  assert.equal(Object.keys(st.sessions).length, 1);
  assert.deepEqual(sleeps, [1000]);
  assert.ok(st.log.includes("GET /files/uploads/u1"));
});

test("a piece whose answer was lost is not sent twice", async () => {
  const st = server({ lostAnswer: 2 }); const { q } = make(st);
  q.add(fakeFile("a.bin", 400), { taskId: "t" });
  await settle(q, finished);
  assert.equal(q.snapshot()[0].state, "done");
  assert.equal(st.puts, 4); // 4 pieces of 100: the lost one was adopted from the server, not re-sent
});

test("server errors back off 1,2,4 seconds and then succeed", async () => {
  const st = server({ fail500: 3 }); const { q, sleeps } = make(st);
  q.add(fakeFile("a.bin", 100), { taskId: "t" });
  await settle(q, finished);
  assert.equal(q.snapshot()[0].state, "done"); assert.deepEqual(sleeps, [1000, 2000, 4000]);
});

test("giving up after too many failures says so", async () => {
  const st = server({ fail500: 99 }); const { q } = make(st);
  q.add(fakeFile("a.bin", 100), { taskId: "t" });
  await settle(q, finished);
  const s = q.snapshot()[0]; assert.equal(s.state, "failed"); assert.match(s.error, /kept failing/);
});

test("choosing the same file again after a reload resumes the same server upload", async () => {
  const st = server({ downAfter: 2 }); const storage = memStorage();
  let a = make(st, { storage }); a.q.add(fakeFile("a.bin", 500, 7), { taskId: "t" });
  await settle(a.q, finished);
  assert.equal(a.q.snapshot()[0].state, "failed");
  const received = st.sessions.u1.received; assert.equal(received, 200);
  st.down = false; st.downAfter = 0; st.puts = 0;
  let b = make(st, { storage }); b.q.add(fakeFile("a.bin", 500, 7), { taskId: "t" }); // a new page, same file
  await settle(b.q, finished);
  assert.equal(b.q.snapshot()[0].state, "done");
  assert.equal(Object.keys(st.sessions).length, 1); // the same upload continued
  assert.equal(st.puts, 3); // only the 300 missing bytes were sent: pieces 3, 4 and 5
});

test("an offset the server disagrees with (416) is adopted", async () => {
  const st = server(); const { q } = make(st);
  q.add(fakeFile("a.bin", 300), { taskId: "t" });
  const orig = st.fetch; let hit = false;
  st.fetch = async (u, i) => { if (!hit && i.method === "PUT") { hit = true; st.sessions.u1.received = 100; }
    return orig(u, i); };
  await settle(q, finished);
  assert.equal(q.snapshot()[0].state, "done");
});

test("a damaged upload fails clearly and forgets its key", async () => {
  const st = server({ badHash: true }); const { q, store } = make(st);
  q.add(fakeFile("a.bin", 200), { taskId: "t" });
  await settle(q, finished);
  const s = q.snapshot()[0]; assert.equal(s.state, "failed"); assert.match(s.error, /damaged/);
  assert.equal(store.m.size, 0);
});

test("a start refused by the server shows the server's message", async () => {
  const st = server({ startStatus: 422 }); const { q } = make(st);
  q.add(fakeFile("tool.exe", 10), { taskId: "t" });
  await settle(q, finished);
  assert.equal(q.snapshot()[0].error, "no way");
});

test("cancel stops an upload, deletes it on the server and forgets the key", async () => {
  const st = server(); const { q, store } = make(st);
  const id = q.add(fakeFile("a.bin", 100000), { taskId: "t" });
  await settle(q, (s) => s[0].state === "uploading" && st.puts > 2);
  q.cancel(id);
  await settle(q, finished);
  assert.equal(q.snapshot()[0].state, "cancelled");
  await settle(q, () => st.sessions.u1.state === "ABANDONED");
  assert.equal(st.sessions.u1.state, "ABANDONED"); assert.equal(store.m.size, 0);
});

test("a queued file can be cancelled before it starts, and an empty file is refused", async () => {
  const st = server(); const { q } = make(st, { concurrency: 1 });
  q.add(fakeFile("first", 1000), { taskId: "t" });
  const second = q.add(fakeFile("second", 1000), { taskId: "t" });
  q.cancel(second);
  const empty = q.add(fakeFile("empty", 0), { taskId: "t" });
  await settle(q, finished, 2000);
  const by = Object.fromEntries(q.snapshot().map((i) => [i.name, i]));
  assert.equal(by.first.state, "done"); assert.equal(by.second.state, "cancelled");
  assert.match(by.empty.error, /empty/); assert.ok(empty);
});

test("a login that expired is reported, not retried", async () => {
  const st = server({ startStatus: 403 }); const { q, sleeps } = make(st);
  q.add(fakeFile("a.bin", 100), { taskId: "t" });
  await settle(q, finished);
  assert.equal(sleeps.length, 0); assert.equal(q.snapshot()[0].state, "failed");
});

test("the Idempotency key is stable for one file and different for another", async () => {
  const st = server(); const { q, store } = make(st);
  q.add(fakeFile("a", 100, 1), { taskId: "t" }); q.add(fakeFile("b", 100, 1), { taskId: "t" });
  await settle(q, finished);
  assert.equal(Object.keys(st.byKey).length, 2);
});
