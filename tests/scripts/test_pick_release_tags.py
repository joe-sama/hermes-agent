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


@pytest.mark.parametrize("remote", [False, True])
def test_commit_matrix_pins_the_same_selected_releases(release_repo, remote):
    args = ["--count", "2", "--merged", "fork", "--include-commits"]
    if remote:
        args += ["--remote", release_repo.as_posix()]
    result = _pick(release_repo, *args)
    assert result.returncode == 0, result.stderr
    records = json.loads(result.stdout)
    assert [record["tag"] for record in records] == ["v2026.1.1", "v2026.1.3"]
    for record in records:
        expected = subprocess.check_output(
            ["git", "-C", str(release_repo), "rev-parse", record["tag"] + "^{commit}"],
            text=True,
        ).strip()
        assert record["commit"] == expected


@pytest.mark.parametrize("case", ["ancestor", "future", "malformed"])
def test_workflow_reuses_only_a_valid_ancestor(release_repo, tmp_path, case):
    import yaml

    workflow = yaml.safe_load((SCRIPT.parents[2] / ".github/workflows/install-e2e-run.yml").read_text(encoding="utf-8"))
    step = next(step for step in workflow["jobs"]["e2e"]["steps"] if step.get("name") == "Validate and reuse the pinned release commit")
    head = subprocess.check_output(["git", "-C", str(release_repo), "rev-parse", "fork"], text=True).strip()
    release = subprocess.check_output(
        ["git", "-C", str(release_repo), "rev-parse", "v2026.1.1" if case == "ancestor" else "v2026.1.4"],
        text=True,
    ).strip()
    if case == "malformed":
        release = "not-a-commit; exit 0"
    output = tmp_path / "github-env.txt"
    env = dict(os.environ, RELEASE_COMMIT=release, GITHUB_SHA=head,
               GITHUB_WORKSPACE=release_repo.as_posix(), GITHUB_ENV=output.as_posix())
    result = subprocess.run([_bash(), "-c", step["run"]], cwd=release_repo, env=env, text=True, capture_output=True)
    if case == "ancestor":
        assert result.returncode == 0, result.stderr
        assert output.read_text().strip() == f"HERMES_DEV_SANDBOX_UPSTREAM={release_repo.as_posix()}"
    else:
        assert result.returncode != 0
        assert not output.exists()
