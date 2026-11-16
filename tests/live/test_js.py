"""M10.3: the browser's live-refresh rules are tested in Node. Skipped, loudly, if Node is not installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed: live refresh code NOT tested")
def test_live_refresh_rules():
    r = subprocess.run(
        [NODE, "--test", "tests/js/live.test.js"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
