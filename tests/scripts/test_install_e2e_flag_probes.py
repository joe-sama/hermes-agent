"""Run the harness's actual flag probes with output larger than a pipe buffer."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

import pytest

from tests.scripts.test_pick_release_tags import _bash


HARNESS = Path(__file__).resolve().parents[2] / "tests/install/install-update-e2e.sh"


def _function(name: str) -> str:
    source = HARNESS.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}", source, re.MULTILINE | re.DOTALL)
    assert match is not None
    return match.group(0)


@pytest.mark.parametrize("function", ["installer_supports", "update_supports"])
@pytest.mark.parametrize("present", [True, False])
@pytest.mark.live_system_guard_bypass
def test_large_output_does_not_turn_a_found_flag_into_sigpipe(tmp_path, function, present):
    # The real sandbox entry point is NOT sourced. The local in_sandbox stub
    # below only cats a fixture and never evaluates its command argument; the
    # guard otherwise mistakes that inert "hermes update --help" text for an
    # update of the developer's checkout. All Git writes are under tmp_path.
    source_dir = tmp_path / "scripts"
    source_dir.mkdir()
    script = source_dir / "install.sh"
    script.write_text("--fixture-flag\n" + "# retained content\n" * 60000, encoding="utf-8", newline="\n")
    for args in (["init", "-b", "main"], ["add", "scripts/install.sh"],
                 ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgSign=false", "commit", "-m", "fixture"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    flag = "--fixture-flag" if present else "--absent-flag"
    invocation = f'{function} HEAD "$FLAG"' if function == "installer_supports" else f'{function} "$FLAG"'
    command = 'set -euo pipefail\nin_sandbox() { cat "$HELP_FILE"; }\n' + _function(function) + "\n" + invocation
    result = subprocess.run(
        [_bash(), "-c", command], cwd=tmp_path,
        env=dict(os.environ, FLAG=flag, HELP_FILE=script.as_posix()),
        text=True, capture_output=True,
    )
    assert result.returncode == (0 if present else 1), result.stderr
    assert "Broken pipe" not in result.stderr
