// SHA-256 must equal Node's crypto for every length around the block edges and for any slicing.
const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("crypto");
const Sha = require("../../app/static/js/sha256.js");

function digest(buf, slices) {
  const h = new Sha(); let p = 0, i = 0;
  while (p < buf.length) { const s = slices[i++ % slices.length]; h.update(new Uint8Array(buf.subarray(p, p + s))); p += s; }
  return h.hex();
}
const want = (buf) => crypto.createHash("sha256").update(buf).digest("hex");

test("known vectors", () => {
  assert.equal(digest(Buffer.alloc(0), [1]), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  assert.equal(digest(Buffer.from("abc"), [1]), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
});

test("every length from 0 to 200 with six different slicings", () => {
  for (let len = 0; len <= 200; len++) {
    const buf = crypto.randomBytes(len);
    for (const sl of [[1], [7], [63, 1], [64], [65], [1000]]) assert.equal(digest(buf, sl), want(buf), `len ${len} ${sl}`);
  }
});

test("padding edges and large inputs", () => {
  for (const len of [55, 56, 57, 63, 64, 119, 120, 121, 100000, 1000003]) {
    const buf = crypto.randomBytes(len);
    assert.equal(digest(buf, [8 * 1024 * 1024, 3]), want(buf), "len " + len);
  }
});
