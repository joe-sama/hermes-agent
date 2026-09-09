"""Exercise release sampling against real Git histories, including forks."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/sandbox/pick-release-tags.sh"


def _bash() -> str:
    if os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/bin/bash.exe"
        if candidate.is_file():
            return str(candidate)
    executable = shutil.which("bash")
    if not executable:
        pytest.skip("The release picker requires Bash")
    return executable


@pytest.fixture
def release_repo(tmp_path):
    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), *args], text=True, stderr=subprocess.PIPE,
        ).strip()

    git("init", "-b", "main")
    git("config", "user.name", "Release Test")
    git("config", "user.email", "release-test@example.invalid")
    for index in range(1, 5):
        git("commit", "--allow-empty", "-m", f"release {index}")
        git("tag", f"v2026.1.{index}")
        if index == 3:
            git("branch", "fork-base")
    git("checkout", "-b", "fork", "fork-base")
    git("commit", "--allow-empty", "-m", "owner changes")
    return tmp_path


def _pick(repo, *args):
    return subprocess.run(
        [_bash(), SCRIPT.as_posix(), "--repo", repo.as_posix(), *args],
        capture_output=True, text=True, check=False,
    )


def test_fork_upgrade_matrix_excludes_newer_upstream_release(release_repo):
    result = _pick(release_repo, "--count", "2", "--merged", "fork")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["v2026.1.1", "v2026.1.3"]


def test_single_slot_selects_latest_eligible_release(release_repo):
    result = _pick(release_repo, "--count", "1", "--merged", "fork")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["v2026.1.3"]


def test_unfiltered_picker_keeps_all_releases(release_repo):
    result = _pick(release_repo, "--count", "9")
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)) == 4


def test_invalid_target_does_not_silently_drop_coverage(release_repo):
    result = _pick(release_repo, "--merged", "no-such-ref")
    assert result.returncode != 0


def test_remote_tag_metadata_works_without_fetching_tags_or_future_history(release_repo):
    # Include an annotated release: ls-remote must use its peeled commit, not
    # mistake the tag-object ID for an ancestor in the fork checkout.
    subprocess.run(
        ["git", "-C", str(release_repo), "-c", "tag.gpgSign=false", "tag", "-fa",
         "v2026.1.3", "fork-base", "-m", "annotated release"],
        check=True, capture_output=True,
    )
    consumer = release_repo / "consumer"
    subprocess.run(
        ["git", "clone", "--no-local", "--no-tags", "--single-branch", "--branch", "fork",
         str(release_repo), str(consumer)],
        check=True, capture_output=True,
    )
    result = _pick(consumer, "--count", "9", "--merged", "HEAD", "--remote", release_repo.as_posix())
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["v2026.1.1", "v2026.1.2", "v2026.1.3"]
    assert subprocess.check_output(["git", "-C", str(consumer), "tag", "--list"], text=True) == ""


def test_remote_mode_requires_a_target(release_repo):
    result = _pick(release_repo, "--remote", release_repo.as_posix())
    assert result.returncode != 0
    assert "requires --merged" in result.stderr
