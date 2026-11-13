/* Hashes a File off the main thread, 8 MiB at a time, so a 200 MB file never freezes the page or fills memory.
   In:  {file}   Out: {progress: 0..1} while working, then {sha256} or {error}. */
importScripts("/static/js/sha256.js");
var SLICE = 8 * 1024 * 1024;
self.onmessage = function (msg) {
  var file = msg.data.file, hash = new Sha256(), pos = 0;
  function next() {
    if (pos >= file.size) { self.postMessage({ sha256: hash.hex() }); return; }
    file.slice(pos, pos + SLICE).arrayBuffer().then(function (buf) {
      hash.update(new Uint8Array(buf));
      pos += buf.byteLength;
      self.postMessage({ progress: file.size ? pos / file.size : 1 });
      next();
    }, function (err) { self.postMessage({ error: String(err && err.message || err) }); });
  }
  next();
};
