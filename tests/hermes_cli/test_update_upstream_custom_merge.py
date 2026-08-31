"""Focused tests for updating customized forks from upstream/main."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import update_cmd


def _result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_custom_fork_and_upstream_ahead_merges_then_pushes(tmp_path, capsys):
    """Diverged fork main receives upstream while retaining its own commits."""
    events = []

    def run(cmd, **_kwargs):
        if "fetch" in cmd:
            events.append("fetch")
            return _result()
        if cmd[-3:] == ["merge", "--no-edit", "upstream/main"]:
            events.append("merge")
            return _result()
        if cmd[-3:] == ["diff", "--name-only", "--diff-filter=U"]:
            events.append("verify-merge")
            return _result()
        if cmd[-3:] == ["push", "origin", "main"]:
            events.append("push")
            return _result()
        raise AssertionError(f"unexpected git command: {cmd}")

    with patch.object(
        update_cmd, "_has_upstream_remote", return_value=True
    ), patch.object(
        update_cmd, "_count_commits_between", side_effect=[2, 3]
    ), patch.object(
        update_cmd.subprocess, "run", side_effect=run
    ):
        checked = update_cmd._sync_with_upstream_if_needed(["git"], tmp_path)

    assert checked is True
    assert events == ["fetch", "merge", "verify-merge", "push"]
    output = capsys.readouterr().out
    assert "Upstream merged; fork commits preserved" in output
    assert "Merged main pushed to origin" in output


def test_custom_fork_merge_conflict_aborts_and_never_pushes(tmp_path, capsys):
    """A merge conflict fails visibly, aborts, and cannot reach push."""
    events = []

    def run(cmd, **_kwargs):
        if "fetch" in cmd:
            events.append("fetch")
            return _result()
        if cmd[-3:] == ["merge", "--no-edit", "upstream/main"]:
            events.append("merge")
            return _result(returncode=1, stderr="automatic merge failed")
        if cmd[-3:] == ["diff", "--name-only", "--diff-filter=U"]:
            events.append("list-conflicts")
            return _result(stdout="agent/prompt_builder.py\n")
        if cmd[-2:] == ["merge", "--abort"]:
            events.append("abort")
            return _result()
        raise AssertionError(f"unexpected git command: {cmd}")

    with patch.object(
        update_cmd, "_has_upstream_remote", return_value=True
    ), patch.object(
        update_cmd, "_count_commits_between", side_effect=[2, 3]
    ), patch.object(
        update_cmd, "_sync_fork_with_upstream"
    ) as push_mock, patch.object(
        update_cmd.subprocess, "run", side_effect=run
    ):
        with pytest.raises(update_cmd.UpstreamSyncError):
            update_cmd._sync_with_upstream_if_needed(["git"], tmp_path)

    assert events == ["fetch", "merge", "list-conflicts", "abort"]
    push_mock.assert_not_called()
    output = capsys.readouterr().out
    assert "Could not merge upstream/main" in output
    assert "agent/prompt_builder.py" in output
    assert "Failed merge aborted" in output
    assert "fork was not pushed" not in output


def test_custom_fork_only_ahead_keeps_existing_skip_behavior(tmp_path, capsys):
    """No upstream commits means no merge and no push for an ahead-only fork."""
    commands = []

    def run(cmd, **_kwargs):
        commands.append(cmd)
        if "fetch" in cmd:
            return _result()
        raise AssertionError(f"unexpected git command: {cmd}")

    with patch.object(
        update_cmd, "_has_upstream_remote", return_value=True
    ), patch.object(
        update_cmd, "_count_commits_between", side_effect=[2, 0]
    ), patch.object(
        update_cmd, "_sync_fork_with_upstream"
    ) as push_mock, patch.object(
        update_cmd.subprocess, "run", side_effect=run
    ):
        checked = update_cmd._sync_with_upstream_if_needed(["git"], tmp_path)

    assert checked is True
    assert all("merge" not in command for command in commands)
    push_mock.assert_not_called()
    output = capsys.readouterr().out
    assert "Skipping upstream sync to preserve your changes" in output
