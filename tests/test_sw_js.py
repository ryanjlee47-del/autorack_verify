"""Runs tests/_sw_test.js under Node -- verifies the service worker's
fetch handler never hands event.respondWith() a null/undefined value.

The failure this guards against is user-visible and misleading: Safari
reports it as "FetchEvent.respondWith received an error: Returned
response is null", which shows on the phone as "Safari can't open the
page" when a worker scans a door QR -- indistinguishable from the server
being down. See static/js/worker/sw.js's offlineFallback() comment.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "_sw_test.js"


def test_service_worker_never_responds_with_null():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node.js is required for this test")
    proc = subprocess.run([node, str(HARNESS)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"service worker JS test failed:\n{proc.stdout}\n{proc.stderr}"
    assert "passed" in proc.stdout
