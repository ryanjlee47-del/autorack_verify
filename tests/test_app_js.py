"""Runs tests/_app_test.js under Node -- covers static/js/worker/app.js's
scan-classification path.

app.js carries the largest share of the worker PWA's behaviour and had no
automated coverage at all: the parity suite stops at barcode.js, and
test_outbox_js.py covers only the queue. That gap is where the review's
false-OK-on-the-dock finding lived -- a wrong item passing as a green OK,
which no server-side test can see because the server never re-verifies an
`ok` result.

The harness stubs a DOM and drives the real file through the wedge-scanner
keydown path, so what runs is the shipped logic rather than a restatement
of it. See _app_test.js's header for what each case pins and why.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "_app_test.js"


def test_worker_app_js_scan_classification():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node.js is required for this test")
    proc = subprocess.run([node, str(HARNESS)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"app.js tests failed:\n{proc.stdout}\n{proc.stderr}"
    assert "all app.js tests passed" in proc.stdout
