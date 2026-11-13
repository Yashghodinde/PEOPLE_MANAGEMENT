"""M08.3: the browser code is tested in Node (the SHA-256 against Node's crypto, the upload queue against a fake
server that speaks the real protocol). Skipped, loudly, if Node is not installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed: the browser upload code was NOT tested")
@pytest.mark.parametrize("name", ["sha256.test.js", "core.test.js"])
def test_browser_code(name):
    r = subprocess.run(
        [NODE, "--test", f"tests/js/{name}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
