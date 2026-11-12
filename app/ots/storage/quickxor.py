"""QuickXorHash: the 160-bit rolling hash OneDrive and SharePoint report in `file.hashes.quickXorHash`.

Microsoft's description: a 160-bit register; each input byte is XORed into the register at bit position
(11 * index) mod 160, wrapping around the end; at the end the byte count is XORed (as 8 little-endian
bytes) into the last 8 bytes of the 20-byte little-endian register; the result is Base64-encoded.

Because XOR is linear and the position repeats every 160 bytes, a block is hashed by XOR-folding its
160-byte rows into one row with big-integer operations (no per-byte Python loop), and only the 160 column
values are rotated into the register at the end. A 1 MiB chunk takes a few milliseconds.
"""

from __future__ import annotations

import base64
import os

WIDTH_BITS = 160
ROW = WIDTH_BITS  # bytes per row: the byte-position pattern repeats every 160 bytes
SHIFT = 11
_MASK = (1 << WIDTH_BITS) - 1


def _fold(rows: int, value: int) -> int:
    """XOR together `rows` consecutive 160-byte rows packed in one integer (little endian)."""
    row_bits = ROW * 8
    n = 1
    while n < rows:
        n <<= 1
    # pad to a power of two rows with zeros (zeros do not change an XOR), then halve repeatedly
    bits = n * row_bits
    while bits > row_bits:
        half = bits // 2
        value = (value & ((1 << half) - 1)) ^ (value >> half)
        bits = half
    return value


class QuickXorHash:
    def __init__(self) -> None:
        self._columns = 0  # 160 bytes (as one int): XOR of all bytes that share a position mod 160
        self._length = 0
        self._carry = b""  # bytes not yet forming a whole row, always < ROW long

    def update(self, data: bytes | bytearray | memoryview) -> None:
        buf = self._carry + bytes(data)
        whole = len(buf) - len(buf) % ROW
        if whole:
            self._columns ^= _fold(whole // ROW, int.from_bytes(buf[:whole], "little"))
        self._carry = buf[whole:]
        self._length += len(data)

    def _final_columns(self) -> int:
        # the last partial row occupies the first columns; the rest are zero
        return self._columns ^ int.from_bytes(self._carry, "little")

    def digest(self) -> bytes:
        columns = self._final_columns()
        register = 0
        for r in range(ROW):
            b = (columns >> (8 * r)) & 0xFF
            if b:
                s = (r * SHIFT) % WIDTH_BITS
                register ^= ((b << s) | (b >> (WIDTH_BITS - s))) & _MASK  # rotate within 160 bits
        out = bytearray(register.to_bytes(WIDTH_BITS // 8, "little"))
        length = self._length.to_bytes(8, "little")
        for i in range(8):
            out[WIDTH_BITS // 8 - 8 + i] ^= length[i]
        return bytes(out)

    def b64digest(self) -> str:
        return base64.b64encode(self.digest()).decode("ascii")


def quickxor_b64(data: bytes) -> str:
    h = QuickXorHash()
    h.update(data)
    return h.b64digest()


def quickxor_file(path: str | os.PathLike[str], chunk: int = 1024 * 1024) -> str:
    """QuickXorHash of a file, read in 1 MiB chunks (never the whole file in memory)."""
    h = QuickXorHash()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.b64digest()
