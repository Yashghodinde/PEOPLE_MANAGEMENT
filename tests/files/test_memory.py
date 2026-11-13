"""M08.2: a 200 MB upload keeps the app's memory growth under 50 MB (the stream is never held whole)."""

import hashlib
import threading
import time
import uuid

import psutil
import pytest
from ots.files import layout, services
from ots.files.models import FileVersion

pytestmark = pytest.mark.django_db
MB = 1024 * 1024
BLOCK = hashlib.sha256(b"seed").digest() * (MB // 32)  # 1 MiB of fixed bytes, repeated: nothing is held whole


class Generated:
    """A request body made on demand: reading it never holds more than the piece asked for."""

    def __init__(self, start: int, length: int) -> None:
        self.pos, self.end = start, start + length

    def read(self, n: int) -> bytes:
        n = min(n, self.end - self.pos)
        if n <= 0:
            return b""
        out = bytearray()
        while len(out) < n:
            offset = (self.pos + len(out)) % MB
            out += BLOCK[offset : offset + n - len(out)]
        self.pos += n
        return bytes(out)


def test_two_hundred_megabytes_grow_memory_by_less_than_fifty(office, settings):
    settings.OTS_UPLOAD_PIECE_BYTES = 8 * MB
    settings.OTS_UPLOAD_MAX_MB = 500
    size = 200 * MB
    h = hashlib.sha256()
    for _ in range(200):
        h.update(BLOCK)
    proc = psutil.Process()
    base = proc.memory_info().rss
    peak = [base]
    stop = threading.Event()

    def watch() -> None:
        while not stop.is_set():
            peak[0] = max(peak[0], proc.memory_info().rss)
            time.sleep(0.01)

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    try:
        user = office["emp"]
        session, _ = services.start_upload(
            user, key=uuid.uuid4(), task=office["task"], file_id=None, assignment_id=None, relation="SUBMISSION",
            original_filename="big.bin", size=size, sha256=h.hexdigest(),
        )  # fmt: skip
        a = 0
        while a < size:
            length = min(8 * MB, size - a)
            progress = services.receive_piece(user, session.pk, a, a + length - 1, size, Generated(a, length))
            a += length
    finally:
        stop.set()
        t.join()
    growth = (peak[0] - base) / MB
    assert progress.version is not None and FileVersion.objects.count() == 1
    assert layout.blob_path(h.hexdigest()).stat().st_size == size
    assert growth < 50, f"memory grew by {growth:.1f} MB"
