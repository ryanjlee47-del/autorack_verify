"""Runs tests/_outbox_test.js under Node -- verifies the outbox's
per-session grouping fix (see static/js/worker/outbox.js's module
docstring): scans left over from a session a worker logged out of must
still sync under their own session_id, never the next session's.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "_outbox_test.js"


def test_outbox_groups_by_session_not_current_session():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node.js is required for this test")
    proc = subprocess.run([node, str(HARNESS)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"outbox JS test failed:\n{proc.stdout}\n{proc.stderr}"
    assert "passed" in proc.stdout
