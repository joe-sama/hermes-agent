"""The Windows console-shim update self-lock (#88838, #89599, #86093).

``venv\\Scripts\\hermes.exe`` is a launcher that runs the interpreter with the
shim itself as its script, keeping the file open without FILE_SHARE_DELETE for
the whole command. An update started that way must therefore replace a file it
is holding, which Windows refuses — so the DEPENDENCY SYNC re-runs itself under
``venv\\Scripts\\python.exe``.

The hand-off sits at the sync boundary, not at the top of ``hermes update``:
everything before it (the fetch, the stash question, the branch switch) runs
in the user's own console, and an up-to-date run that never syncs never hands
off at all.

``_is_windows`` is patched so these paths are exercised on any host.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

import pytest

from hermes_cli import main as cli_main

SHIM_NAMES = ["hermes.exe", "hermes-agent.exe", "hermes-acp.exe", "hermes-gateway.exe"]


@pytest.fixture
def venv(tmp_path, monkeypatch):
    """A Windows-shaped project venv with a python.exe, wired into main."""
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"")
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(cli_main, "_venv_scripts_dir", lambda: scripts)
    monkeypatch.setattr(sys, "argv", ["hermes", "update"])
    monkeypatch.delenv(cli_main._UPDATE_REEXEC_ENV, raising=False)
    monkeypatch.setattr(cli_main, "_UPDATE_EXIT_RECOVERIES", [])
    monkeypatch.setattr(cli_main, "_UPDATE_TRANSFERABLE_RECOVERIES", [])
    monkeypatch.setattr(cli_main, "_UPDATE_REEXEC_PARENT_HARD_EXIT", False)
    monkeypatch.setattr(cli_main, "_UPDATE_REEXEC_PENDING_MARKER_TRANSFER", None)
    monkeypatch.setattr(cli_main, "_UPDATE_RECOVERY_ACK_COMMITTED", False)
    monkeypatch.setattr(cli_main, "_UPDATE_RECOVERY_OWNERSHIP_COMMITTED", False)
    monkeypatch.setattr(
        "hermes_cli.update_lock.transfer_update_marker", lambda *_a, **_k: True
    )
    _fake_psutil(monkeypatch, [])
    return scripts


def _fake_psutil(monkeypatch, ancestor_exes: list[str]):
    """Stand in for psutil with a fixed self+ancestor executable chain."""

    class _Proc:
        def __init__(self, exe=None):
            self._exe = exe

        def exe(self):
            if self._exe is None:
                raise OSError("exe unavailable")
            return self._exe

        def parents(self):
            return [_Proc(exe) for exe in ancestor_exes]

        def create_time(self):
            return 1234.5

    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(Process=_Proc))


def _capture_popen(monkeypatch, raises: Exception | None = None):
    calls = []

    class Child:
        pid = 7330

        def __init__(self, child_env):
            self.child_env = child_env

        def poll(self):
            accept_path = Path(
                self.child_env[cli_main._UPDATE_RECOVERY_ACCEPT_ENV]
            )
            if accept_path.exists():
                accepted = json.loads(accept_path.read_text(encoding="utf-8"))
                Path(self.child_env[cli_main._UPDATE_RECOVERY_ACK_ENV]).write_text(
                    json.dumps(
                        {
                            "nonce": accepted["nonce"],
                            "pid": self.pid,
                            "state": "committed",
                        }
                    ),
                    encoding="utf-8",
                )
            return None

    def fake_popen(cmd, env=None, **kwargs):
        if raises is not None:
            raise raises
        child_env = dict(env or {})
        calls.append((list(cmd), child_env, kwargs))
        payload = json.loads(
            Path(child_env[cli_main._UPDATE_RECOVERY_TRANSFER_ENV]).read_text(
                encoding="utf-8"
            )
        )
        Path(child_env[cli_main._UPDATE_RECOVERY_ACK_ENV]).write_text(
            json.dumps(
                {"nonce": payload["nonce"], "pid": Child.pid, "state": "ready"}
            ),
            encoding="utf-8",
        )
        return Child(child_env)

    monkeypatch.setattr(cli_main.subprocess, "Popen", fake_popen)
    return calls


def _seed_child_handoff(monkeypatch, tmp_path, recoveries, *, parent_pid=4242):
    transfer_path = tmp_path / "recoveries.json"
    ack_path = tmp_path / "recoveries.ack"
    accept_path = tmp_path / "recoveries.accepted"
    nonce = "test-handoff-nonce"
    transfer_path.write_text(
        json.dumps(
            {
                "version": 1,
                "nonce": nonce,
                "parent_pid": parent_pid,
                "parent_create_time": 1234.5,
                "recoveries": recoveries,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(cli_main._UPDATE_RECOVERY_TRANSFER_ENV, str(transfer_path))
    monkeypatch.setenv(cli_main._UPDATE_RECOVERY_ACK_ENV, str(ack_path))
    monkeypatch.setenv(cli_main._UPDATE_RECOVERY_ACCEPT_ENV, str(accept_path))
    monkeypatch.setattr(
        "hermes_cli.update_lock.marker_handoff_state",
        lambda *_a, **_k: "successor",
    )
    return transfer_path, ack_path, accept_path, nonce


# ---------------------------------------------------------------------------
# Shim detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shim_name", SHIM_NAMES)
def test_detects_shim_as_argv0(venv, monkeypatch, shim_name):
    monkeypatch.setattr(sys, "argv", [str(venv / shim_name), "update"])
    assert cli_main._windows_shim_in_process_chain() == venv / shim_name


def test_detects_shim_from_zipapp_main_py(venv, monkeypatch):
    """runpy/zipapp launches put ``<shim>\\__main__.py`` in argv[0]."""
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe" / "__main__.py")])
    assert cli_main._windows_shim_in_process_chain() == venv / "hermes.exe"


def test_detects_shim_from_main_module_spec_origin(venv, monkeypatch):
    fake_main = types.SimpleNamespace(
        __file__=None,
        __spec__=types.SimpleNamespace(origin=str(venv / "hermes.exe")),
    )
    monkeypatch.setitem(sys.modules, "__main__", fake_main)
    assert cli_main._windows_shim_in_process_chain() == venv / "hermes.exe"


def test_detects_shim_in_ancestor_chain(venv, monkeypatch):
    """The launcher is usually a separate parent process, not argv[0]."""
    _fake_psutil(monkeypatch, [str(venv / "hermes.exe")])
    assert cli_main._windows_shim_in_process_chain() == venv / "hermes.exe"


def test_ignores_hermes_exe_outside_the_project_venv(venv, monkeypatch, tmp_path):
    """A shim from some other install must never trigger a re-exec."""
    other = tmp_path / "other" / "Scripts"
    other.mkdir(parents=True)
    monkeypatch.setattr(sys, "argv", [str(other / "hermes.exe"), "update"])
    _fake_psutil(monkeypatch, [str(other / "hermes.exe")])
    assert cli_main._windows_shim_in_process_chain() is None


def test_no_shim_off_windows(venv, monkeypatch):
    monkeypatch.setattr(cli_main, "_is_windows", lambda: False)
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    assert cli_main._windows_shim_in_process_chain() is None


def test_no_shim_without_a_venv(venv, monkeypatch):
    monkeypatch.setattr(cli_main, "_venv_scripts_dir", lambda: None)
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    assert cli_main._windows_shim_in_process_chain() is None


# ---------------------------------------------------------------------------
# Re-exec hand-off
# ---------------------------------------------------------------------------


def test_reexec_runs_same_args_under_venv_python(venv, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update", "--yes"])
    calls = _capture_popen(monkeypatch)

    assert cli_main._reexec_dependency_sync_off_windows_shim() is True
    cmd, env, kwargs = calls[0]
    assert cmd == [
        str(venv / "python.exe"), "-m", "hermes_cli.main", "update", "--yes",
    ]
    assert env[cli_main._UPDATE_REEXEC_ENV] == "1"
    assert cli_main._UPDATE_RECOVERY_TRANSFER_ENV in env
    assert cli_main._UPDATE_RECOVERY_ACK_ENV in env
    assert cli_main._UPDATE_RECOVERY_ACCEPT_ENV in env
    accepted = json.loads(
        Path(env[cli_main._UPDATE_RECOVERY_ACCEPT_ENV]).read_text(encoding="utf-8")
    )
    assert accepted["nonce"]
    assert accepted["pid"] == 7330
    assert accepted["parent_pid"] == os.getpid()
    assert cli_main._UPDATE_REEXEC_PARENT_HARD_EXIT is True
    assert "under the venv Python" in capsys.readouterr().out


def test_reexec_child_runs_unattended(venv, monkeypatch):
    """The parent exits, so a prompt in the child could never be answered."""
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    calls = _capture_popen(monkeypatch)

    assert cli_main._reexec_dependency_sync_off_windows_shim() is True
    assert calls[0][2]["stdin"] is cli_main.subprocess.DEVNULL


def test_reexec_does_not_recurse(venv, monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    monkeypatch.setenv(cli_main._UPDATE_REEXEC_ENV, "1")
    calls = _capture_popen(monkeypatch)

    assert cli_main._reexec_dependency_sync_off_windows_shim() is False
    assert calls == []


def test_reexec_skipped_when_not_launched_from_a_shim(venv, monkeypatch):
    calls = _capture_popen(monkeypatch)
    assert cli_main._reexec_dependency_sync_off_windows_shim() is False
    assert calls == []


def test_reexec_falls_through_when_venv_python_is_missing(venv, monkeypatch, capsys):
    (venv / "python.exe").unlink()
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])

    assert cli_main._reexec_dependency_sync_off_windows_shim() is False
    assert "-m hermes_cli.main update" not in capsys.readouterr().out


def test_reexec_falls_through_when_spawn_fails(venv, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    _capture_popen(monkeypatch, raises=OSError("no exec"))

    assert cli_main._reexec_dependency_sync_off_windows_shim() is False
    assert "-m hermes_cli.main update" in capsys.readouterr().out


def test_reexec_transfers_hindsight_and_serve_ownership_after_valid_ack(
    venv, monkeypatch
):
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    cli_main._UPDATE_EXIT_RECOVERIES.clear()
    cli_main._UPDATE_TRANSFERABLE_RECOVERIES.clear()
    monkeypatch.setattr(cli_main, "_UPDATE_REEXEC_PARENT_HARD_EXIT", False)
    gateway_token = {
        "resume_needed": True,
        "profiles": {"default": 1111},
        "unmapped_pids": [2222],
        "unmapped": [
            {
                "pid": 2222,
                "argv": ["python.exe", "-m", "hermes_cli.main", "gateway", "run"],
            }
        ],
        "services": ["HermesGateway"],
    }
    hindsight_token = {
        "pending": True,
        "entries": [
            {
                "profile": "hermes",
                "port": 9177,
                "hindsight_home": str(venv.parent / "hindsight"),
                "hermes_root": str(venv.parent / "hermes"),
                "profile_env": "C:/Users/example/.hindsight/profiles/hermes.env",
                "api_key": "must-not-cross-processes",
            }
        ],
    }
    serve_token = {
        "pending": True,
        "entries": [
            {
                "purpose": "serve",
                "host": "127.0.0.1",
                "port": 9119,
                "profile": "",
                "argv": "must-not-be-replayed-or-transferred",
            }
        ],
    }
    cli_main._register_update_exit_recovery(
        cli_main._resume_windows_gateways_after_update,
        gateway_token,
        transfer_kind="gateway",
    )
    cli_main._register_update_exit_recovery(
        cli_main._relaunch_stopped_hindsight_daemons,
        hindsight_token,
        transfer_kind="hindsight",
    )
    cli_main._register_update_exit_recovery(
        cli_main._relaunch_stopped_serves,
        serve_token,
        transfer_kind="serve",
    )
    captured: dict[str, object] = {}

    class Child:
        pid = 7331
        terminated = False
        child_env = None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

        def poll(self):
            if self.child_env is not None:
                accept_path = Path(
                    self.child_env[cli_main._UPDATE_RECOVERY_ACCEPT_ENV]
                )
                if accept_path.exists():
                    accepted = json.loads(accept_path.read_text(encoding="utf-8"))
                    Path(
                        self.child_env[cli_main._UPDATE_RECOVERY_ACK_ENV]
                    ).write_text(
                        json.dumps(
                            {
                                "nonce": accepted["nonce"],
                                "pid": self.pid,
                                "state": "committed",
                            }
                        ),
                        encoding="utf-8",
                    )
            return None

    child = Child()

    def fake_popen(cmd, env=None, **kwargs):
        child_env = dict(env or {})
        transfer_path = Path(child_env[cli_main._UPDATE_RECOVERY_TRANSFER_ENV])
        payload = json.loads(transfer_path.read_text(encoding="utf-8"))
        ack_path = Path(child_env[cli_main._UPDATE_RECOVERY_ACK_ENV])
        ack_path.write_text(
            json.dumps(
                {"nonce": payload["nonce"], "pid": child.pid, "state": "ready"}
            ),
            encoding="utf-8",
        )
        child.child_env = child_env
        captured.update(cmd=list(cmd), env=child_env, payload=payload, kwargs=kwargs)
        return child

    monkeypatch.setattr(cli_main.subprocess, "Popen", fake_popen)
    try:
        assert cli_main._reexec_dependency_sync_off_windows_shim() is True
    finally:
        cli_main._UPDATE_EXIT_RECOVERIES.clear()
        cli_main._UPDATE_TRANSFERABLE_RECOVERIES.clear()

    payload = captured["payload"]
    assert payload["version"] == 1
    assert payload["parent_pid"] == os.getpid()
    assert payload["parent_create_time"] == 1234.5
    assert {row["kind"] for row in payload["recoveries"]} == {
        "gateway",
        "hindsight",
        "serve",
    }
    gateway_payload = next(
        row["token"] for row in payload["recoveries"] if row["kind"] == "gateway"
    )
    assert gateway_payload == gateway_token | {"resume_needed": True}
    serialized = json.dumps(payload)
    assert "must-not-cross-processes" not in serialized
    assert "must-not-be-replayed-or-transferred" not in serialized
    assert hindsight_token["pending"] is False
    assert serve_token["pending"] is False
    assert gateway_token["resume_needed"] is False
    assert cli_main._UPDATE_REEXEC_PARENT_HARD_EXIT is True
    assert child.terminated is False


def test_reexec_missing_ack_keeps_parent_recovery_ownership(venv, monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    cli_main._UPDATE_EXIT_RECOVERIES.clear()
    cli_main._UPDATE_TRANSFERABLE_RECOVERIES.clear()
    monkeypatch.setattr(cli_main, "_UPDATE_REEXEC_PARENT_HARD_EXIT", False)
    monkeypatch.setattr(cli_main, "_UPDATE_RECOVERY_ACK_TIMEOUT_SECONDS", 0.0)
    token = {
        "pending": True,
        "entries": [
            {
                "profile": "hermes",
                "port": 9177,
                "hindsight_home": str(venv.parent / "hindsight"),
                "hermes_root": str(venv.parent / "hermes"),
            }
        ],
    }
    gateway_token = {
        "resume_needed": True,
        "profiles": {"default": 1111},
        "unmapped_pids": [],
        "unmapped": [],
    }
    cli_main._register_update_exit_recovery(
        cli_main._resume_windows_gateways_after_update,
        gateway_token,
        transfer_kind="gateway",
    )
    cli_main._register_update_exit_recovery(
        cli_main._relaunch_stopped_hindsight_daemons,
        token,
        transfer_kind="hindsight",
    )

    class Child:
        pid = 7332
        terminated = False
        waited = False

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.waited = True
            return 0

        def poll(self):
            return 0 if self.waited else None

    child = Child()
    monkeypatch.setattr(cli_main.subprocess, "Popen", lambda *a, **k: child)
    try:
        assert cli_main._reexec_dependency_sync_off_windows_shim() is False
    finally:
        cli_main._UPDATE_EXIT_RECOVERIES.clear()
        cli_main._UPDATE_TRANSFERABLE_RECOVERIES.clear()

    assert token["pending"] is True
    assert gateway_token["resume_needed"] is True
    assert cli_main._UPDATE_REEXEC_PARENT_HARD_EXIT is False
    assert child.terminated is True
    assert child.waited is True


def test_reexec_refuses_to_transfer_a_partial_recovery_token(venv, monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    token = {
        "pending": True,
        "entries": [
            {
                "profile": "hermes",
                "port": 9177,
                "hindsight_home": str(venv.parent / "hindsight"),
                "hermes_root": str(venv.parent / "hermes"),
            },
            {"profile": "broken", "port": 0},
        ],
    }
    cli_main._register_update_exit_recovery(
        cli_main._relaunch_stopped_hindsight_daemons,
        token,
        transfer_kind="hindsight",
    )
    calls = _capture_popen(monkeypatch)

    assert cli_main._reexec_dependency_sync_off_windows_shim() is False
    assert calls == []
    assert token["pending"] is True
    assert cli_main._UPDATE_REEXEC_PARENT_HARD_EXIT is False


def test_child_adopts_all_recoveries_before_acknowledging(monkeypatch, tmp_path):
    nonce = "test-nonce"
    transfer_path = tmp_path / "recoveries.json"
    ack_path = tmp_path / "recoveries.ack.json"
    accept_path = tmp_path / "recoveries.accepted.json"
    transfer_path.write_text(
        json.dumps(
            {
                "version": 1,
                "nonce": nonce,
                "parent_pid": os.getpid(),
                "parent_create_time": 1234.5,
                "recoveries": [
                    {
                        "kind": "gateway",
                        "token": {
                            "resume_needed": True,
                            "profiles": {"default": 1111},
                            "unmapped_pids": [],
                            "unmapped": [],
                        },
                    },
                    {
                        "kind": "hindsight",
                        "entries": [
                            {
                                "profile": "hermes",
                                "port": 9177,
                                "hindsight_home": str(tmp_path / "hindsight"),
                                "hermes_root": str(tmp_path / "hermes"),
                            }
                        ],
                    },
                    {
                        "kind": "serve",
                        "entries": [
                            {
                                "purpose": "serve",
                                "host": "127.0.0.1",
                                "port": 9119,
                                "profile": "",
                            }
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(cli_main._UPDATE_RECOVERY_TRANSFER_ENV, str(transfer_path))
    monkeypatch.setenv(cli_main._UPDATE_RECOVERY_ACK_ENV, str(ack_path))
    monkeypatch.setenv(cli_main._UPDATE_RECOVERY_ACCEPT_ENV, str(accept_path))
    monkeypatch.setattr(cli_main, "_ADOPTED_WINDOWS_GATEWAY_RESUME", None)
    monkeypatch.setattr(cli_main, "_UPDATE_RECOVERY_ACK_COMMITTED", False)
    monkeypatch.setattr(cli_main, "_UPDATE_RECOVERY_OWNERSHIP_COMMITTED", False)
    registrations: list[tuple[str, dict, str]] = []
    atexit_registrations: list[tuple[str, dict]] = []

    def fake_register(callback, token, *, transfer_kind):
        assert not ack_path.exists(), "child ACKed before command-exit recovery registration"
        registrations.append((callback.__name__, token, transfer_kind))

    def fake_atexit_register(callback, token):
        assert not ack_path.exists(), "child ACKed before atexit recovery registration"
        atexit_registrations.append((callback.__name__, token))

    monkeypatch.setattr(cli_main, "_register_update_exit_recovery", fake_register)
    monkeypatch.setattr("atexit.register", fake_atexit_register)
    monkeypatch.setattr("hermes_cli.update_lock.read_live_update", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.update_lock.marker_handoff_state",
        lambda *_a, **_k: "successor",
    )
    monkeypatch.setattr(cli_main, "_update_handoff_parent_alive", lambda *_a: False)

    assert cli_main._adopt_transferred_update_recoveries() is True
    assert [kind for _name, _token, kind in registrations] == [
        "gateway",
        "hindsight",
        "serve",
    ]
    assert [name for name, _token in atexit_registrations] == [
        "_resume_windows_gateways_after_update",
        "_relaunch_stopped_hindsight_daemons",
        "_relaunch_stopped_serves",
    ]
    assert all(
        token["resume_needed"] is True
        if kind == "gateway"
        else token["pending"] is True
        for _name, token, kind in registrations
    )
    adopted_gateway = cli_main._take_adopted_windows_gateway_resume()
    assert adopted_gateway == {
        "resume_needed": True,
        "profiles": {"default": 1111},
        "unmapped_pids": [],
        "unmapped": [],
    }
    assert cli_main._take_adopted_windows_gateway_resume() is None
    assert cli_main._UPDATE_RECOVERY_ACK_COMMITTED is True
    assert cli_main._UPDATE_RECOVERY_OWNERSHIP_COMMITTED is True
    assert not ack_path.exists()
    assert cli_main._UPDATE_RECOVERY_TRANSFER_ENV not in os.environ
    assert cli_main._UPDATE_RECOVERY_ACK_ENV not in os.environ
    assert cli_main._UPDATE_RECOVERY_ACCEPT_ENV not in os.environ


def test_ready_without_parent_acceptance_never_arms_child(monkeypatch, tmp_path):
    recoveries = [
        {
            "kind": "serve",
            "entries": [
                {
                    "purpose": "serve",
                    "host": "127.0.0.1",
                    "port": 9119,
                    "profile": "",
                }
            ],
        }
    ]
    _seed_child_handoff(monkeypatch, tmp_path, recoveries)
    registered = []
    monkeypatch.setattr(
        cli_main,
        "_register_update_exit_recovery",
        lambda callback, token, *, transfer_kind: registered.append(
            (callback, token, transfer_kind)
        ),
    )
    monkeypatch.setattr("atexit.register", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.update_lock.read_live_update",
        lambda: types.SimpleNamespace(pid=4242),
    )
    monkeypatch.setattr(cli_main, "_update_handoff_parent_alive", lambda *_a: True)
    ticks = iter([0.0, 0.0, 31.0])
    monkeypatch.setattr(cli_main._time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(cli_main._time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="parent did not release"):
        cli_main._adopt_transferred_update_recoveries()

    assert cli_main._UPDATE_RECOVERY_ACK_COMMITTED is True
    assert cli_main._UPDATE_RECOVERY_OWNERSHIP_COMMITTED is False
    assert registered[0][1]["pending"] is False


def test_ready_publication_failure_keeps_child_inert(monkeypatch, tmp_path):
    recoveries = [
        {
            "kind": "serve",
            "entries": [
                {
                    "purpose": "serve",
                    "host": "127.0.0.1",
                    "port": 9119,
                    "profile": "",
                }
            ],
        }
    ]
    _seed_child_handoff(monkeypatch, tmp_path, recoveries)
    registered = []
    monkeypatch.setattr(
        cli_main,
        "_register_update_exit_recovery",
        lambda callback, token, *, transfer_kind: registered.append(
            (callback, token, transfer_kind)
        ),
    )
    monkeypatch.setattr("atexit.register", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.update_lock.read_live_update", lambda: None
    )
    monkeypatch.setattr(
        cli_main,
        "_publish_update_recovery_child_state",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("ack refused")),
    )

    with pytest.raises(OSError, match="ack refused"):
        cli_main._adopt_transferred_update_recoveries()

    assert cli_main._UPDATE_RECOVERY_ACK_COMMITTED is False
    assert cli_main._UPDATE_RECOVERY_OWNERSHIP_COMMITTED is False
    assert registered[0][1]["pending"] is False


def test_committed_state_is_never_visible_with_child_ownership_false(
    monkeypatch, tmp_path
):
    parent_pid = 4242
    recoveries = [
        {
            "kind": "serve",
            "entries": [
                {
                    "purpose": "serve",
                    "host": "127.0.0.1",
                    "port": 9119,
                    "profile": "",
                }
            ],
        }
    ]
    _transfer, ack_path, accept_path, nonce = _seed_child_handoff(
        monkeypatch, tmp_path, recoveries, parent_pid=parent_pid
    )
    accept_path.write_text(
        json.dumps(
            {"nonce": nonce, "pid": os.getpid(), "parent_pid": parent_pid}
        ),
        encoding="utf-8",
    )
    registered = []
    monkeypatch.setattr(
        cli_main,
        "_register_update_exit_recovery",
        lambda callback, token, *, transfer_kind: registered.append(
            (callback, token, transfer_kind)
        ),
    )
    monkeypatch.setattr("atexit.register", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.update_lock.read_live_update",
        lambda: types.SimpleNamespace(pid=parent_pid),
    )
    monkeypatch.setattr(cli_main, "_update_handoff_parent_alive", lambda *_a: True)
    real_publish = cli_main._publish_update_recovery_child_state
    observed = []

    def publish_then_interrupt(path, child_nonce, state):
        real_publish(path, child_nonce, state)
        if state == "committed":
            observed.append(json.loads(ack_path.read_text(encoding="utf-8")))
            raise KeyboardInterrupt

    monkeypatch.setattr(
        cli_main, "_publish_update_recovery_child_state", publish_then_interrupt
    )

    with pytest.raises(KeyboardInterrupt):
        cli_main._adopt_transferred_update_recoveries()

    assert observed[0]["state"] == "committed"
    assert cli_main._UPDATE_RECOVERY_OWNERSHIP_COMMITTED is True
    assert registered[0][1]["pending"] is True


def test_postcommit_interrupt_with_temporarily_unreadable_ack_keeps_child_armed(
    monkeypatch, tmp_path
):
    """An ambiguous ACK read cannot revoke already-published ownership."""
    parent_pid = 4242
    recoveries = [
        {
            "kind": "serve",
            "entries": [
                {
                    "purpose": "serve",
                    "host": "127.0.0.1",
                    "port": 9119,
                    "profile": "",
                }
            ],
        }
    ]
    _transfer, ack_path, accept_path, nonce = _seed_child_handoff(
        monkeypatch, tmp_path, recoveries, parent_pid=parent_pid
    )
    accept_path.write_text(
        json.dumps({"nonce": nonce, "pid": os.getpid(), "parent_pid": parent_pid}),
        encoding="utf-8",
    )
    registered = []
    monkeypatch.setattr(
        cli_main,
        "_register_update_exit_recovery",
        lambda callback, token, *, transfer_kind: registered.append(
            (callback, token, transfer_kind)
        ),
    )
    monkeypatch.setattr("atexit.register", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.update_lock.read_live_update",
        lambda: types.SimpleNamespace(pid=parent_pid),
    )
    monkeypatch.setattr(cli_main, "_update_handoff_parent_alive", lambda *_a: True)
    real_publish = cli_main._publish_update_recovery_child_state
    real_read_text = Path.read_text
    commit_was_published = False

    def publish_then_interrupt(path, child_nonce, state):
        nonlocal commit_was_published
        real_publish(path, child_nonce, state)
        if state == "committed":
            commit_was_published = True
            raise KeyboardInterrupt

    def temporarily_unreadable(path, *args, **kwargs):
        if path == ack_path and commit_was_published:
            raise OSError("sharing violation")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(
        cli_main, "_publish_update_recovery_child_state", publish_then_interrupt
    )
    monkeypatch.setattr(Path, "read_text", temporarily_unreadable)

    with pytest.raises(KeyboardInterrupt):
        cli_main._adopt_transferred_update_recoveries()

    assert cli_main._UPDATE_RECOVERY_OWNERSHIP_COMMITTED is True
    assert registered[0][1]["pending"] is True
    assert ack_path.exists(), "live parent still needs the durable COMMITTED ACK"


def test_interrupt_before_committed_publication_disarms_child(monkeypatch, tmp_path):
    """The arming micro-boundary is protected even before the write begins."""
    parent_pid = 4242
    recoveries = [
        {
            "kind": "serve",
            "entries": [
                {
                    "purpose": "serve",
                    "host": "127.0.0.1",
                    "port": 9119,
                    "profile": "",
                }
            ],
        }
    ]
    _transfer, _ack_path, accept_path, nonce = _seed_child_handoff(
        monkeypatch, tmp_path, recoveries, parent_pid=parent_pid
    )
    accept_path.write_text(
        json.dumps({"nonce": nonce, "pid": os.getpid(), "parent_pid": parent_pid}),
        encoding="utf-8",
    )
    registered = []
    monkeypatch.setattr(
        cli_main,
        "_register_update_exit_recovery",
        lambda callback, token, *, transfer_kind: registered.append(
            (callback, token, transfer_kind)
        ),
    )
    monkeypatch.setattr("atexit.register", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.update_lock.read_live_update",
        lambda: types.SimpleNamespace(pid=parent_pid),
    )
    monkeypatch.setattr(cli_main, "_update_handoff_parent_alive", lambda *_a: True)
    real_publish = cli_main._publish_update_recovery_child_state

    def interrupt_before_commit(path, child_nonce, state):
        if state == "committed":
            assert cli_main._UPDATE_RECOVERY_OWNERSHIP_COMMITTED is True
            assert registered[0][1]["pending"] is True
            raise KeyboardInterrupt
        real_publish(path, child_nonce, state)

    monkeypatch.setattr(
        cli_main, "_publish_update_recovery_child_state", interrupt_before_commit
    )

    with pytest.raises(KeyboardInterrupt):
        cli_main._adopt_transferred_update_recoveries()

    assert cli_main._UPDATE_RECOVERY_OWNERSHIP_COMMITTED is False
    assert registered[0][1]["pending"] is False


def test_parent_death_mid_multitoken_arm_forces_every_callback_live(
    monkeypatch, tmp_path
):
    """Once the exact parent dies, a partial arm must become a full takeover."""
    recoveries = [
        {
            "kind": "hindsight",
            "entries": [
                {
                    "profile": "hermes",
                    "port": 9177,
                    "hindsight_home": str(tmp_path / "hindsight"),
                    "hermes_root": str(tmp_path / "hermes"),
                }
            ],
        },
        {
            "kind": "serve",
            "entries": [
                {
                    "purpose": "serve",
                    "host": "127.0.0.1",
                    "port": 9119,
                    "profile": "",
                }
            ],
        },
    ]
    _seed_child_handoff(monkeypatch, tmp_path, recoveries)
    monkeypatch.setattr("atexit.register", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.update_lock.read_live_update", lambda: None)
    monkeypatch.setattr(cli_main, "_update_handoff_parent_alive", lambda *_a: False)
    recovered = []
    monkeypatch.setattr(
        cli_main,
        "_relaunch_stopped_hindsight_daemons",
        lambda token: recovered.append(("hindsight", token["pending"])) or True,
    )
    monkeypatch.setattr(
        cli_main,
        "_relaunch_stopped_serves",
        lambda token: recovered.append(("serve", token["pending"])) or True,
    )
    cli_main._UPDATE_EXIT_RECOVERIES.clear()
    cli_main._UPDATE_TRANSFERABLE_RECOVERIES.clear()
    interrupted = False

    def interrupt_after_first_token(frame, event, arg):
        nonlocal interrupted
        if (
            not interrupted
            and event == "line"
            and frame.f_code.co_name == "_set_adopted_enabled"
            and frame.f_locals.get("enabled") is True
        ):
            adopted = frame.f_locals.get("adopted")
            if (
                adopted
                and len(adopted) == 2
                and adopted[0][1].get("pending") is True
                and adopted[1][1].get("pending") is False
            ):
                interrupted = True
                sys.settrace(None)
                raise KeyboardInterrupt
        return interrupt_after_first_token

    try:
        sys.settrace(interrupt_after_first_token)
        with pytest.raises(KeyboardInterrupt):
            cli_main._adopt_transferred_update_recoveries()
    finally:
        sys.settrace(None)

    assert interrupted is True
    assert cli_main._UPDATE_RECOVERY_OWNERSHIP_COMMITTED is True
    assert cli_main._run_update_exit_recoveries() is True
    assert sorted(recovered) == [("hindsight", True), ("serve", True)]
    cli_main._UPDATE_EXIT_RECOVERIES.clear()
    cli_main._UPDATE_TRANSFERABLE_RECOVERIES.clear()


def test_parent_death_before_lock_transfer_recovers_and_aborts(monkeypatch, tmp_path):
    recoveries = [
        {
            "kind": "serve",
            "entries": [
                {
                    "purpose": "serve",
                    "host": "127.0.0.1",
                    "port": 9119,
                    "profile": "",
                }
            ],
        }
    ]
    _seed_child_handoff(monkeypatch, tmp_path, recoveries)
    registered = []
    monkeypatch.setattr(
        cli_main,
        "_register_update_exit_recovery",
        lambda callback, token, *, transfer_kind: registered.append(
            (callback, token, transfer_kind)
        ),
    )
    monkeypatch.setattr("atexit.register", lambda *a, **k: None)
    monkeypatch.setattr(cli_main, "_update_handoff_parent_alive", lambda *_a: False)
    monkeypatch.setattr(
        "hermes_cli.update_lock.marker_handoff_state",
        lambda *_a, **_k: "source",
    )
    monkeypatch.setattr(
        "hermes_cli.update_lock.read_live_update",
        lambda *_a, **_k: pytest.fail("handoff must not delete a dead-parent marker"),
    )

    with pytest.raises(RuntimeError, match="before atomic lock transfer"):
        cli_main._adopt_transferred_update_recoveries()

    assert cli_main._UPDATE_RECOVERY_OWNERSHIP_COMMITTED is True
    assert registered[0][1]["pending"] is True


def test_parent_death_after_lock_transfer_allows_child_to_continue(
    monkeypatch, tmp_path
):
    recoveries = [
        {
            "kind": "serve",
            "entries": [
                {
                    "purpose": "serve",
                    "host": "127.0.0.1",
                    "port": 9119,
                    "profile": "",
                }
            ],
        }
    ]
    _seed_child_handoff(monkeypatch, tmp_path, recoveries)
    monkeypatch.setattr("atexit.register", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_main,
        "_register_update_exit_recovery",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(cli_main, "_update_handoff_parent_alive", lambda *_a: False)

    assert cli_main._adopt_transferred_update_recoveries() is True
    assert cli_main._UPDATE_RECOVERY_OWNERSHIP_COMMITTED is True


def test_committed_child_state_is_authoritative_after_child_exit(tmp_path):
    ack_path = tmp_path / "recoveries.ack"
    ack_path.write_text(
        json.dumps({"nonce": "n", "pid": 7330, "state": "committed"}),
        encoding="utf-8",
    )

    class Child:
        pid = 7330

        @staticmethod
        def poll():
            return 2

    assert cli_main._wait_for_update_recovery_commit(ack_path, "n", Child()) is True


def test_commit_wait_retries_transient_ack_read_after_child_exit(
    monkeypatch, tmp_path
):
    ack_path = tmp_path / "recoveries.ack"
    ack_path.write_text(
        json.dumps({"nonce": "n", "pid": 7330, "state": "committed"}),
        encoding="utf-8",
    )
    real_read_text = Path.read_text
    reads = 0

    def fail_first_read(path, *args, **kwargs):
        nonlocal reads
        if path == ack_path:
            reads += 1
            if reads == 1:
                raise OSError("transient sharing violation")
        return real_read_text(path, *args, **kwargs)

    ticks = iter([0.0, 0.0, 0.0])
    monkeypatch.setattr(Path, "read_text", fail_first_read)
    monkeypatch.setattr(cli_main._time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(cli_main._time, "sleep", lambda _seconds: None)

    class Child:
        pid = 7330

        @staticmethod
        def poll():
            return 2

    assert cli_main._wait_for_update_recovery_commit(ack_path, "n", Child()) is True
    assert reads == 2


def test_parent_interrupt_during_disarm_stops_child_and_rearms(
    venv, monkeypatch
):
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])

    class InterruptOnce(dict):
        interrupted = False

        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if key == "pending" and value is False and not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt

    token = InterruptOnce(
        pending=True,
        entries=[
            {
                "profile": "hermes",
                "port": 9177,
                "hindsight_home": str(venv.parent / "hindsight"),
                "hermes_root": str(venv.parent / "hermes"),
            }
        ],
    )
    cli_main._register_update_exit_recovery(
        cli_main._relaunch_stopped_hindsight_daemons,
        token,
        transfer_kind="hindsight",
    )
    _capture_popen(monkeypatch)
    stopped = []
    monkeypatch.setattr(
        cli_main,
        "_stop_unacknowledged_update_child",
        lambda process: stopped.append(process.pid) or "dead",
    )

    try:
        with pytest.raises(KeyboardInterrupt):
            cli_main._reexec_dependency_sync_off_windows_shim()
    finally:
        cli_main._UPDATE_EXIT_RECOVERIES.clear()
        cli_main._UPDATE_TRANSFERABLE_RECOVERIES.clear()

    assert stopped == [7330]
    assert token["pending"] is True
    assert cli_main._UPDATE_REEXEC_PARENT_HARD_EXIT is False


@pytest.mark.parametrize("stop_state", ["live", "unknown"])
def test_unacknowledged_child_must_be_confirmed_dead_before_parent_continues(
    venv, monkeypatch, stop_state
):
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    monkeypatch.setattr(cli_main, "_UPDATE_RECOVERY_ACK_TIMEOUT_SECONDS", 0.0)
    token = {
        "pending": True,
        "entries": [
            {
                "profile": "hermes",
                "port": 9177,
                "hindsight_home": str(venv.parent / "hindsight"),
                "hermes_root": str(venv.parent / "hermes"),
            }
        ],
    }
    cli_main._register_update_exit_recovery(
        cli_main._relaunch_stopped_hindsight_daemons,
        token,
        transfer_kind="hindsight",
    )

    class Child:
        pid = 7335

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(cli_main.subprocess, "Popen", lambda *a, **k: Child())
    monkeypatch.setattr(
        cli_main, "_stop_unacknowledged_update_child", lambda _process: stop_state
    )
    try:
        with pytest.raises(RuntimeError, match=stop_state):
            cli_main._reexec_dependency_sync_off_windows_shim()
    finally:
        cli_main._UPDATE_EXIT_RECOVERIES.clear()
        cli_main._UPDATE_TRANSFERABLE_RECOVERIES.clear()

    assert token["pending"] is True
    assert cli_main._UPDATE_REEXEC_PARENT_HARD_EXIT is False


def test_committed_child_lock_transfer_failure_stops_and_rearms_parent(
    venv, monkeypatch
):
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    token = {
        "pending": True,
        "entries": [
            {
                "profile": "hermes",
                "port": 9177,
                "hindsight_home": str(venv.parent / "hindsight"),
                "hermes_root": str(venv.parent / "hermes"),
            }
        ],
    }
    cli_main._register_update_exit_recovery(
        cli_main._relaunch_stopped_hindsight_daemons,
        token,
        transfer_kind="hindsight",
    )
    _capture_popen(monkeypatch)
    stopped = []
    monkeypatch.setattr(
        "hermes_cli.update_lock.transfer_update_marker", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        cli_main,
        "_stop_unacknowledged_update_child",
        lambda process: stopped.append(process.pid) or "dead",
    )
    try:
        assert cli_main._reexec_dependency_sync_off_windows_shim() is False
    finally:
        cli_main._UPDATE_EXIT_RECOVERIES.clear()
        cli_main._UPDATE_TRANSFERABLE_RECOVERIES.clear()

    assert stopped == [7330]
    assert token["pending"] is True
    assert cli_main._UPDATE_REEXEC_PARENT_HARD_EXIT is False


def test_dead_committed_child_never_receives_lock_or_recovery_ownership(
    venv, monkeypatch
):
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    token = {
        "pending": True,
        "entries": [
            {
                "profile": "hermes",
                "port": 9177,
                "hindsight_home": str(venv.parent / "hindsight"),
                "hermes_root": str(venv.parent / "hermes"),
            }
        ],
    }
    cli_main._register_update_exit_recovery(
        cli_main._relaunch_stopped_hindsight_daemons,
        token,
        transfer_kind="hindsight",
    )

    class DeadCommittedChild:
        pid = 7336

        @staticmethod
        def poll():
            return 9

    monkeypatch.setattr(
        cli_main.subprocess, "Popen", lambda *_a, **_k: DeadCommittedChild()
    )
    monkeypatch.setattr(cli_main, "_wait_for_update_recovery_ack", lambda *_a: True)
    monkeypatch.setattr(
        cli_main, "_publish_update_recovery_acceptance", lambda *_a: None
    )
    monkeypatch.setattr(cli_main, "_wait_for_update_recovery_commit", lambda *_a: True)
    monkeypatch.setattr(
        "hermes_cli.update_lock.transfer_update_marker",
        lambda *_a, **_k: pytest.fail("dead child must never receive update marker"),
    )

    try:
        assert cli_main._reexec_dependency_sync_off_windows_shim() is False
    finally:
        cli_main._UPDATE_EXIT_RECOVERIES.clear()
        cli_main._UPDATE_TRANSFERABLE_RECOVERIES.clear()

    assert token["pending"] is True
    assert cli_main._UPDATE_REEXEC_PARENT_HARD_EXIT is False


def test_interrupt_after_atomic_marker_replace_selects_parent_hard_exit(
    venv, monkeypatch
):
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    token = {
        "pending": True,
        "entries": [
            {
                "profile": "hermes",
                "port": 9177,
                "hindsight_home": str(venv.parent / "hindsight"),
                "hermes_root": str(venv.parent / "hermes"),
            }
        ],
    }
    cli_main._register_update_exit_recovery(
        cli_main._relaunch_stopped_hindsight_daemons,
        token,
        transfer_kind="hindsight",
    )
    _capture_popen(monkeypatch)
    replaced = []

    def interrupt_after_replace(*_args, **_kwargs):
        replaced.append(True)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "hermes_cli.update_lock.transfer_update_marker", interrupt_after_replace
    )
    monkeypatch.setattr(
        "hermes_cli.update_lock.marker_handoff_state",
        lambda *_args, **_kwargs: "successor",
    )
    try:
        assert cli_main._reexec_dependency_sync_off_windows_shim() is True
    finally:
        cli_main._UPDATE_EXIT_RECOVERIES.clear()
        cli_main._UPDATE_TRANSFERABLE_RECOVERIES.clear()

    assert replaced == [True]
    assert token["pending"] is False
    assert cli_main._UPDATE_REEXEC_PARENT_HARD_EXIT is True


@pytest.mark.parametrize("stop_state", ["live", "unknown"])
def test_committed_child_lock_transfer_failure_never_continues_with_live_child(
    venv, monkeypatch, stop_state
):
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    token = {
        "pending": True,
        "entries": [
            {
                "profile": "hermes",
                "port": 9177,
                "hindsight_home": str(venv.parent / "hindsight"),
                "hermes_root": str(venv.parent / "hermes"),
            }
        ],
    }
    cli_main._register_update_exit_recovery(
        cli_main._relaunch_stopped_hindsight_daemons,
        token,
        transfer_kind="hindsight",
    )
    _capture_popen(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.update_lock.transfer_update_marker", lambda *_a, **_k: False
    )
    monkeypatch.setattr(
        cli_main, "_stop_unacknowledged_update_child", lambda _process: stop_state
    )
    try:
        with pytest.raises(RuntimeError, match=stop_state):
            cli_main._reexec_dependency_sync_off_windows_shim()
    finally:
        cli_main._UPDATE_EXIT_RECOVERIES.clear()
        cli_main._UPDATE_TRANSFERABLE_RECOVERIES.clear()

    assert token["pending"] is False
    assert cli_main._UPDATE_REEXEC_PARENT_HARD_EXIT is True


def test_parent_reclaims_when_ready_child_dies_before_committed(
    venv, monkeypatch
):
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    token = {
        "pending": True,
        "entries": [
            {
                "profile": "hermes",
                "port": 9177,
                "hindsight_home": str(venv.parent / "hindsight"),
                "hermes_root": str(venv.parent / "hermes"),
            }
        ],
    }
    cli_main._register_update_exit_recovery(
        cli_main._relaunch_stopped_hindsight_daemons,
        token,
        transfer_kind="hindsight",
    )
    captured = {}

    class Child:
        pid = 7334
        child_env = None

        def poll(self):
            if self.child_env is None:
                return None
            accept_path = Path(
                self.child_env[cli_main._UPDATE_RECOVERY_ACCEPT_ENV]
            )
            return 2 if accept_path.exists() else None

    child = Child()

    def fake_popen(cmd, env=None, **kwargs):
        child_env = dict(env or {})
        payload = json.loads(
            Path(child_env[cli_main._UPDATE_RECOVERY_TRANSFER_ENV]).read_text(
                encoding="utf-8"
            )
        )
        Path(child_env[cli_main._UPDATE_RECOVERY_ACK_ENV]).write_text(
            json.dumps(
                {"nonce": payload["nonce"], "pid": child.pid, "state": "ready"}
            ),
            encoding="utf-8",
        )
        child.child_env = child_env
        captured.update(env=child_env)
        return child

    monkeypatch.setattr(cli_main.subprocess, "Popen", fake_popen)
    try:
        assert cli_main._reexec_dependency_sync_off_windows_shim() is False
    finally:
        cli_main._UPDATE_EXIT_RECOVERIES.clear()
        cli_main._UPDATE_TRANSFERABLE_RECOVERIES.clear()

    assert token["pending"] is True
    assert cli_main._UPDATE_REEXEC_PARENT_HARD_EXIT is False
    assert not Path(captured["env"][cli_main._UPDATE_RECOVERY_ACCEPT_ENV]).exists()


def test_accepted_handoff_status_print_is_best_effort(venv, monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    _capture_popen(monkeypatch)
    real_print = print

    def closed_stream_print(*args, **kwargs):
        if args and "Windows:" in str(args[0]):
            raise ValueError("I/O operation on closed file")
        return real_print(*args, **kwargs)

    monkeypatch.setattr("builtins.print", closed_stream_print)
    assert cli_main._reexec_dependency_sync_off_windows_shim() is True


def test_adopted_gateway_token_is_not_paused_or_registered_twice(monkeypatch):
    from hermes_cli import update_cmd

    class PastGuard(Exception):
        pass

    class RootSentinel:
        def __truediv__(self, _other):
            raise PastGuard

    token = {
        "resume_needed": True,
        "profiles": {"default": 1111},
        "unmapped_pids": [],
        "unmapped": [],
    }
    pause_calls: list[object] = []
    register_calls: list[object] = []
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    monkeypatch.setattr(cli_main, "_venv_scripts_dir", lambda: None)
    monkeypatch.setattr(cli_main, "_run_pre_update_backup", lambda _args: None)
    monkeypatch.setattr(
        cli_main, "_take_adopted_windows_gateway_resume", lambda: token
    )
    monkeypatch.setattr(
        cli_main,
        "_pause_windows_gateways_for_update",
        lambda: pause_calls.append(object()),
    )
    monkeypatch.setattr(
        cli_main,
        "_register_update_exit_recovery",
        lambda *a, **k: register_calls.append((a, k)),
    )
    monkeypatch.setattr(cli_main, "_detect_venv_python_processes", lambda: [])
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", RootSentinel())
    monkeypatch.setattr("hermes_cli.update_receipt.record_step", lambda *a, **k: None)
    args = types.SimpleNamespace(
        gateway=False,
        check=False,
        no_backup=True,
        backup=False,
        yes=True,
        branch=None,
        force=False,
        force_venv=False,
    )

    with pytest.raises(PastGuard):
        update_cmd._cmd_update_impl(args, gateway_mode=False)

    assert pause_calls == []
    assert register_calls == []
    assert token["resume_needed"] is True


# ---------------------------------------------------------------------------
# Hand-off placement: the sync boundary, not the top of the command
# ---------------------------------------------------------------------------


def test_up_to_date_run_never_hands_off(venv, monkeypatch, capsys):
    """The regression that started this: a no-op update must not detach.

    The hand-off used to run before the fetch, so every ``hermes update`` —
    including the ``Already up to date!`` case that never touches the venv —
    spawned a child and returned to the shell, leaving the child printing
    into a console it no longer owned. ``--check`` is the cheapest real run
    that reaches ``cmd_update`` and exits without syncing; nothing may be
    spawned along the way.
    """
    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update", "--check"])
    calls = _capture_popen(monkeypatch)
    monkeypatch.setattr(cli_main, "_cmd_update_check", lambda **kwargs: None)

    cli_main.cmd_update(types.SimpleNamespace(check=True, branch=None))

    assert calls == [], "an up-to-date run must not spawn a detached child"


def test_sync_guard_hands_off_when_only_the_shim_is_held(venv, monkeypatch):
    """No native module mapped, but we ARE the shim: hand off and exit 0."""
    from hermes_cli import update_cmd

    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    monkeypatch.setattr(cli_main, "_detect_self_loaded_native_modules", lambda: [])
    calls = _capture_popen(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        update_cmd._abort_dependency_sync_if_self_locked()

    assert excinfo.value.code == 0
    assert calls, "expected the dependency sync to be handed to the venv python"


def test_sync_guard_defers_native_lock_before_considering_the_shim(venv, monkeypatch):
    """A mapped .pyd still exits 2 — the marker recovery owns that case."""
    from hermes_cli import update_cmd

    monkeypatch.setattr(sys, "argv", [str(venv / "hermes.exe"), "update"])
    monkeypatch.setattr(
        cli_main, "_detect_self_loaded_native_modules", lambda: ["PyYAML (_yaml.pyd)"]
    )
    monkeypatch.setattr(cli_main, "_defer_update_for_self_lock", lambda loaded: None)
    calls = _capture_popen(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        update_cmd._abort_dependency_sync_if_self_locked()

    assert excinfo.value.code == 2
    assert calls == [], "a native-module deferral must not also spawn a child"


def test_sync_guard_is_a_noop_when_nothing_is_held(venv, monkeypatch):
    """Off the shim with nothing mapped, the sync just proceeds in-process."""
    from hermes_cli import update_cmd

    monkeypatch.setattr(cli_main, "_detect_self_loaded_native_modules", lambda: [])
    calls = _capture_popen(monkeypatch)

    update_cmd._abort_dependency_sync_if_self_locked()
    assert calls == []


# ---------------------------------------------------------------------------
# Reboot-deferred renames
# ---------------------------------------------------------------------------


def test_reboot_deferred_rename_fallback_is_gone():
    """MOVEFILE_DELAY_UNTIL_REBOOT needed elevation and freed nothing."""
    assert not hasattr(cli_main, "_schedule_replace_on_reboot")


def test_pending_rename_filter_drops_only_our_shim_pairs():
    shims = [Path(r"C:\hermes\venv\Scripts\hermes.exe")]
    entries = [
        r"\??\C:\other\thing.dll", r"!\??\C:\other\thing.dll.bak",
        r"\??\C:\hermes\venv\Scripts\hermes.exe",
        r"!\??\C:\hermes\venv\Scripts\hermes.exe.old.1755624735000",
    ]
    kept, removed = cli_main._filter_pending_shim_renames(entries, shims)
    assert removed == 1
    assert kept == entries[:2]


def test_pending_rename_filter_keeps_a_shim_pair_with_a_foreign_target():
    shims = [Path(r"C:\hermes\venv\Scripts\hermes.exe")]
    entries = [
        r"\??\C:\hermes\venv\Scripts\hermes.exe", r"!\??\C:\somewhere\else.exe",
    ]
    kept, removed = cli_main._filter_pending_shim_renames(entries, shims)
    assert removed == 0
    assert kept == entries


def test_pending_rename_filter_preserves_a_trailing_delete_entry():
    """A bare source with an empty target is a scheduled delete, not a pair."""
    entries = [r"\??\C:\other\thing.dll", "", r"\??\C:\other\orphan.dll"]
    kept, removed = cli_main._filter_pending_shim_renames(entries, [])
    assert removed == 0
    assert kept == entries


# ---------------------------------------------------------------------------
# venv layout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("venv_name", ["venv", ".venv"])
def test_venv_scripts_dir_finds_both_layouts(tmp_path, monkeypatch, venv_name):
    """uv writes .venv; our installers write venv. Both must resolve (#79542)."""
    scripts = tmp_path / venv_name / "Scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    assert cli_main._venv_scripts_dir() == scripts
