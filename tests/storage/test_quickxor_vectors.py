"""QuickXorHash against values Microsoft Graph itself reported (recorded by deploy/scripts/graph_live_check.py).

The data is regenerated from a seed, so the real service's answer is checked without storing any files.
"""

import hashlib
import json
import logging
from pathlib import Path

import pytest
from ots.storage.quickxor import quickxor_b64

VECTORS = json.loads((Path(__file__).parent / "vectors.json").read_text())["vectors"]


def pattern(seed: str, size: int) -> bytes:
    """Same generator as deploy/scripts/graph_live_check.py."""
    out = bytearray()
    i = 0
    while len(out) < size:
        out += hashlib.sha256(seed.encode() + i.to_bytes(8, "big")).digest()
        i += 1
    return bytes(out[:size])


def test_there_are_vectors_from_the_real_service_including_the_4_mib_boundary():
    sizes = {v["size"] for v in VECTORS}
    assert len(VECTORS) >= 10 and {1, 160, 161, 4 * 1024 * 1024} <= sizes


@pytest.mark.parametrize("vector", VECTORS, ids=lambda v: f"{v['size']}-bytes")
def test_our_hash_equals_the_one_microsoft_reported(vector):
    assert quickxor_b64(pattern(vector["seed"], vector["size"])) == vector["quickxor"]


def test_http_request_urls_are_not_logged(settings):
    """A Graph uploadUrl holds a temporary token; httpx's per-request INFO line would put it in the logs."""
    from django.conf import settings as s

    assert s.LOGGING["loggers"]["httpx"]["level"] == "WARNING"
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
