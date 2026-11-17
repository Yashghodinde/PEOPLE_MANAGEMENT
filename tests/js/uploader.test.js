// The Alpine component's own logic (what the buttons do), with Alpine and the browser stubbed out.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

function load() {
  let factory = null, onInit = null;
  const added = [], renamed = [];
  let coreOpts = null;
  global.document = { addEventListener: (n, fn) => { if (n === "alpine:init") onInit = fn; }, body: { getAttribute: () => "{}" } };
  global.window = { fetch: () => {}, Worker: undefined, crypto: {} };
  global.Alpine = { data: (name, f) => { if (name === "uploader") factory = f; } };
  global.OtsUploadCore = { create: (opts) => (coreOpts = opts, { rename: (id, name) => renamed.push([id, name]), add: (file, meta) => added.push({ name: file.name, ...meta }), cancel() {}, clearFinished() {} }) };
  global.Sha256 = function () {};
  new Function(fs.readFileSync(path.join(__dirname, "../../app/static/js/uploader.js"), "utf8"))();
  onInit();
  const c = factory();
  const clicks = [];
  c.$el = { dataset: { taskId: "task-1", defaultRelation: "SUBMISSION" } };
  c.$refs = { input: { click: () => clicks.push(1) } };
  c.init();
  return { c, added, clicks, renamed, coreOpts: () => coreOpts };
}
const files = (...names) => names.map((n) => ({ name: n, size: 5 }));

test("choosing files uploads new files for the task with the default kind", () => {
  const { c, added } = load();
  c.picked({ target: { files: files("a.xlsx", "b.xlsx"), value: "x" } });
  assert.deepEqual(added.map((a) => [a.name, a.taskId, a.relation, a.fileId]), [["a.xlsx", "task-1", "SUBMISSION", ""], ["b.xlsx", "task-1", "SUBMISSION", ""]]);
});

test("'Upload a new version' sends the next file as a version of that file, once", () => {
  const { c, added, clicks } = load();
  c.newVersion({ currentTarget: { dataset: { fileId: "file-A" } } });
  assert.equal(clicks.length, 1);
  c.picked({ target: { files: files("v2.xlsx"), value: "" } });
  c.picked({ target: { files: files("other.xlsx"), value: "" } });
  assert.equal(added[0].fileId, "file-A"); assert.equal(added[1].fileId, "");
});

test("a cancelled 'new version' dialog does not turn the next 'Choose files' into a new version", () => {
  const { c, added } = load();
  c.newVersion({ currentTarget: { dataset: { fileId: "file-A" } } }); // the person then cancels the dialog: no change event
  c.choose();
  c.picked({ target: { files: files("fresh.xlsx"), value: "" } });
  assert.equal(added[0].fileId, "");
});

test("a cancelled 'new version' dialog does not turn dropped files into new versions either", () => {
  const { c, added } = load();
  c.newVersion({ currentTarget: { dataset: { fileId: "file-A" } } });
  c.dropped({ dataTransfer: { files: files("dropped.xlsx") } });
  assert.equal(added[0].fileId, ""); assert.equal(c.dragging, false);
});

test("choosing 'new version' for a second file replaces the first choice", () => {
  const { c, added } = load();
  c.newVersion({ currentTarget: { dataset: { fileId: "file-A" } } });
  c.newVersion({ currentTarget: { dataset: { fileId: "file-B" } } });
  c.picked({ target: { files: files("x.xlsx"), value: "" } });
  assert.equal(added[0].fileId, "file-B");
});

test("'Send with this name' hands the typed name to the queue for that file", () => {
  const { c, renamed } = load();
  const row = { dataset: { id: "7" }, querySelector: () => ({ value: "2026-11-17_Budget.pdf" }) };
  c.sendName({ currentTarget: { closest: () => row } });
  assert.deepEqual(renamed, [[7, "2026-11-17_Budget.pdf"]]);
});

test("the queue is given a name check that calls the server's check-name endpoint", async () => {
  const { coreOpts } = load();
  const calls = [];
  global.fetch = async (url, init) => (calls.push([url, JSON.parse(init.body)]), { ok: true, json: async () => ({ ok: false }) });
  const v = await coreOpts().checkName("Untitled.pdf", "task-1");
  assert.deepEqual(calls, [["/files/check-name", { name: "Untitled.pdf", task_id: "task-1" }]]);
  assert.equal(v.ok, false);
});
