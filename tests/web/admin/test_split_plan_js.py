"""The split editor's plan logic (split_plan.js) is tested with node's built-in runner."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_split_plan_js():
    result = subprocess.run(
        ["node", "--test", "tests/js/split_plan.test.mjs"], cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
