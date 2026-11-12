"""QuickXorHash: matches a literal per-byte implementation of Microsoft's description at every size."""

import base64
import os

import pytest
from ots.storage.quickxor import QuickXorHash, quickxor_b64


def reference(data: bytes) -> str:
    """The published algorithm written the slow, obvious way."""
    register, mask = 0, (1 << 160) - 1
    for position, byte in enumerate(data):
        shift = (position * 11) % 160
        register ^= ((byte << shift) | (byte >> (160 - shift))) & mask
    out = bytearray(register.to_bytes(20, "little"))
    length = len(data).to_bytes(8, "little")
    for i in range(8):
        out[12 + i] ^= length[i]
    return base64.b64encode(bytes(out)).decode()


def test_empty_input_is_all_zero():
    assert quickxor_b64(b"") == "AAAAAAAAAAAAAAAAAAAAAAAAAAA="


@pytest.mark.parametrize("n", [1, 2, 7, 8, 159, 160, 161, 319, 320, 321, 1000, 4096, 65535, 100_000])
def test_matches_the_literal_algorithm(n):
    data = os.urandom(n)
    assert quickxor_b64(data) == reference(data)


@pytest.mark.parametrize("sizes", [(1,), (160,), (159, 1, 161), (500, 3, 7000), (1, 1, 1, 1)])
def test_chunking_does_not_change_the_hash(sizes):
    data = os.urandom(sum(sizes))
    h, i = QuickXorHash(), 0
    for s in sizes:
        h.update(data[i : i + s])
        i += s
    assert h.b64digest() == reference(data)


def test_length_is_part_of_the_hash():
    assert quickxor_b64(b"\x00") != quickxor_b64(b"\x00\x00")  # all-zero data of different lengths differ
