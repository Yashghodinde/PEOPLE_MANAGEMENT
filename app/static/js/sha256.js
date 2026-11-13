/* Incremental SHA-256 (FIPS 180-4). crypto.subtle.digest cannot hash in pieces, and a 200 MB file must not be
   held in memory, so the hash is fed 8 MiB slices. Constants are derived, not typed, and the tests compare
   the result with Node's crypto for many lengths and slice sizes. Works in a worker, a page and Node. */
(function (root) {
  "use strict";
  var K = new Int32Array(64), H0 = new Int32Array(8);
  (function () {
    var n = 2, count = 0;
    while (count < 64) {
      var prime = true;
      for (var i = 2; i * i <= n; i++) if (n % i === 0) { prime = false; break; }
      if (prime) {
        if (count < 8) H0[count] = Math.floor((Math.sqrt(n) % 1) * 4294967296);
        K[count] = Math.floor((Math.cbrt(n) % 1) * 4294967296);
        count++;
      }
      n++;
    }
  })();

  function ror(x, n) { return (x >>> n) | (x << (32 - n)); }

  function Sha256() {
    this.h = new Int32Array(H0);
    this.w = new Int32Array(64);
    this.buf = new Uint8Array(64);
    this.used = 0;
    this.length = 0; // bytes fed so far
  }

  Sha256.prototype._block = function (b, o) {
    var w = this.w, h = this.h, i;
    for (i = 0; i < 16; i++, o += 4) w[i] = (b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3];
    for (i = 16; i < 64; i++) {
      var x = w[i - 15], y = w[i - 2];
      var s0 = ror(x, 7) ^ ror(x, 18) ^ (x >>> 3), s1 = ror(y, 17) ^ ror(y, 19) ^ (y >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
    }
    var a = h[0], bb = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (i = 0; i < 64; i++) {
      var t1 = (hh + (ror(e, 6) ^ ror(e, 11) ^ ror(e, 25)) + ((e & f) ^ (~e & g)) + K[i] + w[i]) | 0;
      var t2 = ((ror(a, 2) ^ ror(a, 13) ^ ror(a, 22)) + ((a & bb) ^ (a & c) ^ (bb & c))) | 0;
      hh = g; g = f; f = e; e = (d + t1) | 0; d = c; c = bb; bb = a; a = (t1 + t2) | 0;
    }
    h[0] = (h[0] + a) | 0; h[1] = (h[1] + bb) | 0; h[2] = (h[2] + c) | 0; h[3] = (h[3] + d) | 0;
    h[4] = (h[4] + e) | 0; h[5] = (h[5] + f) | 0; h[6] = (h[6] + g) | 0; h[7] = (h[7] + hh) | 0;
  };

  Sha256.prototype.update = function (bytes) {
    var pos = 0, n = bytes.length;
    this.length += n;
    if (this.used) {
      var take = Math.min(64 - this.used, n);
      this.buf.set(bytes.subarray(0, take), this.used);
      this.used += take; pos = take;
      if (this.used < 64) return this;
      this._block(this.buf, 0); this.used = 0;
    }
    for (; pos + 64 <= n; pos += 64) this._block(bytes, pos); // straight from the input: no copy
    if (pos < n) { this.buf.set(bytes.subarray(pos), 0); this.used = n - pos; }
    return this;
  };

  Sha256.prototype.hex = function () {
    var bits = this.length * 8, pad = new Uint8Array(((this.used < 56 ? 56 : 120) - this.used) + 8);
    pad[0] = 0x80;
    var hi = Math.floor(bits / 4294967296), lo = bits >>> 0, k = pad.length - 8;
    pad[k] = hi >>> 24; pad[k + 1] = hi >>> 16; pad[k + 2] = hi >>> 8; pad[k + 3] = hi;
    pad[k + 4] = lo >>> 24; pad[k + 5] = lo >>> 16; pad[k + 6] = lo >>> 8; pad[k + 7] = lo;
    var keep = this.length;
    this.update(pad);
    this.length = keep;
    var out = "";
    for (var i = 0; i < 8; i++) out += ("00000000" + (this.h[i] >>> 0).toString(16)).slice(-8);
    return out;
  };

  if (typeof module !== "undefined" && module.exports) module.exports = Sha256;
  else root.Sha256 = Sha256;
})(typeof self !== "undefined" ? self : this);
