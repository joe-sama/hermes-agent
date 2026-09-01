"""Legacy pythonw launcher normalization + post-update launcher refresh.

Covers the two halves of the "legacy pythonw gateways survive updates
forever" gap:

1. ``gateway_windows._resolve_detached_python`` — normalizes a legacy
   ``pythonw.exe`` interpreter (pre-aa2ae36c3f launchers / argv snapshots)
   to the sibling console ``python.exe`` so respawns and regenerated
   launchers use the hidden-console design (#54220/#56747) and don't die
   with ``RuntimeError: sys.stderr is None`` (#71671).
2. ``hermes_cli.main._refresh_windows_gateway_launchers`` — ``hermes
   update`` regenerates the installed Scheduled Task / Startup launcher
   scripts instead of leaving install-time artifacts stale forever.

``_resolve_detached_python`` is a pure path helper and runs on any host.
``windowless_gateway_restart_spec`` returns its argv unchanged off Windows,
so the test that exercises the rewrite is ``windows_only`` rather than run
against a faked ``sys.platform``.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

import hermes_cli.gateway_windows as gateway_windows
import hermes_cli.main as cli_main


# ---------------------------------------------------------------------------
# _resolve_detached_python: legacy pythonw normalization
# ---------------------------------------------------------------------------


def _make_venv(tmp_path: Path, *, with_console_python: bool) -> tuple[Path, Path]:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    pythonw = scripts / "pythonw.exe"
    pythonw.write_text("", encoding="utf-8")
    python = scripts / "python.exe"
    if with_console_python:
        python.write_text("", encoding="utf-8")
    return pythonw, python


def test_resolve_detached_python_swaps_legacy_pythonw_for_console_sibling(tmp_path):
    pythonw, python = _make_venv(tmp_path, with_console_python=True)

    exe, venv_dir, extra = gateway_windows._resolve_detached_python(str(pythonw))

    assert exe == str(python)
    assert venv_dir == tmp_path / "venv"
    assert extra == []




@pytest.mark.windows_only
def test_restart_spec_normalizes_legacy_pythonw_argv(tmp_path):
    """A pre-rework Scheduled Task argv snapshot (leading pythonw.exe) must be
    respawned through the console python + hidden-console launch, with every
    argument after the interpreter preserved verbatim.

    ``windows_only``: ``windowless_gateway_restart_spec`` returns the argv
    untouched off Windows, so the fake was the only thing making the rewrite
    (and its ``Scripts/``-layout venv derivation) run at all.
    """
    pythonw, python = _make_venv(tmp_path, with_console_python=True)

    argv = [str(pythonw), "-m", "hermes_cli.main", "gateway", "run"]
    with mock.patch.object(
        gateway_windows, "_stable_gateway_working_dir", return_value=str(tmp_path)
    ), mock.patch("hermes_cli.config.get_hermes_home", return_value=str(tmp_path)):
        new_argv, cwd, env = gateway_windows.windowless_gateway_restart_spec(list(argv))

    assert new_argv[0] == str(python)
    assert new_argv[1:] == argv[1:]
    assert cwd == str(tmp_path)
    assert env["VIRTUAL_ENV"] == str(tmp_path / "venv")


# ---------------------------------------------------------------------------
# _refresh_windows_gateway_launchers: hermes update regenerates launchers
# ---------------------------------------------------------------------------


@pytest.mark.windows_only
def test_refresh_rewrites_inner_launchers_without_replacing_startup_gate(
    tmp_path, monkeypatch
):
    """A normal update must leave owner-managed Startup policy intact.

    ``hermes update`` owns the generated launchers under ``gateway-service``.
    It does not own a custom Startup-folder wrapper that may wait for local
    dependencies before chaining to that inner VBS.  Exercise the real
    ``_write_task_script`` filesystem path so a future call to
    ``_install_startup_entry`` cannot hide behind a mocked writer.
    """
    hermes_home = tmp_path / "hermes"
    gateway_service = hermes_home / "gateway-service"
    gateway_service.mkdir(parents=True)
    inner_cmd = gateway_service / "Hermes_Gateway.cmd"
    inner_vbs = inner_cmd.with_suffix(".vbs")
    inner_cmd.write_text("stale cmd\n", encoding="utf-8")
    inner_vbs.write_text("stale vbs\n", encoding="utf-8")

    startup_dir = tmp_path / "Startup"
    startup_dir.mkdir()
    startup_gate = startup_dir / "Hermes_Gateway.vbs"
    gate_content = (
        "' owner dependency gate\r\n"
        "' wait for authenticated model and Hindsight, then chain inward\r\n"
    )
    startup_gate.write_text(gate_content, encoding="utf-8", newline="")

    python = hermes_home / "hermes-agent" / "venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")

    import hermes_cli.config as config
    import hermes_cli.gateway as gateway

    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(gateway_windows, "is_installed", lambda: True)
    monkeypatch.setattr(gateway_windows, "get_task_script_path", lambda: inner_cmd)
    # Redirect the Startup path too: if refresh regresses into install logic,
    # it must alter this fixture and fail the preservation assertion below.
    monkeypatch.setattr(
        gateway_windows, "get_startup_entry_path", lambda: startup_gate
    )
    monkeypatch.setattr(config, "get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(gateway, "get_python_path", lambda: str(python))
    monkeypatch.setattr(gateway, "PROJECT_ROOT", hermes_home / "hermes-agent")
    monkeypatch.setattr(gateway, "_profile_arg", lambda _home=None: "")

    cli_main._refresh_windows_gateway_launchers()

    with startup_gate.open(encoding="utf-8", newline="") as fh:
        assert fh.read() == gate_content
    assert inner_cmd.read_text(encoding="utf-8") != "stale cmd\n"
    assert inner_vbs.read_text(encoding="utf-8") != "stale vbs\n"
    assert "gateway run" in inner_cmd.read_text(encoding="utf-8")
    assert "gateway run" in inner_vbs.read_text(encoding="utf-8")






