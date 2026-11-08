"""The UI state rule for the missing-answer note, run as a plain node test.

Kept as a node test because it tests the browser module directly, with no framework:
node --test tests/js/notes.test.mjs
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS_TEST = ROOT / "tests" / "js" / "notes.test.mjs"


def test_ui_state_rule():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed, so the JS test cannot run here")
    result = subprocess.run([node, "--test", str(JS_TEST)], capture_output=True, text=True)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
