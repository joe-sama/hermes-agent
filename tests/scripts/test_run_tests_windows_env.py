"""Windows location variables survive the canonical runner's clean environment."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests.scripts.test_pick_release_tags import _bash


@pytest.mark.windows_only
def test_clean_runner_preserves_system_drive_without_forwarding_credentials():
    source = (Path(__file__).resolve().parents[2] / "scripts/run_tests.sh").read_text(encoding="utf-8")
    location_block = source.split("WIN_ENV=()", 1)[1].split("# ── Test-runner knobs", 1)[0]
    command = (
        "set -euo pipefail\nWIN_ENV=()\n" + location_block
        + '\nenv -i "${WIN_ENV[@]}" "$TEST_PYTHON" -c '
        + "'import json, os; print(json.dumps(dict(os.environ)))'"
    )
    result = subprocess.run(
        [_bash(), "-c", command],
        env=dict(os.environ, SYSTEMDRIVE=os.environ["SystemDrive"],
                 TEST_PYTHON=sys.executable, OPENROUTER_API_KEY="must-not-be-forwarded"),
        text=True, capture_output=True, check=True,
    )
    child = {name.upper(): value for name, value in json.loads(result.stdout).items()}
    assert child["SYSTEMDRIVE"] == os.environ["SystemDrive"]
    assert child["SYSTEMROOT"] == os.environ["SystemRoot"]
    assert "OPENROUTER_API_KEY" not in child
