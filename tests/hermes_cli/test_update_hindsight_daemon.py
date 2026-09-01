"""Windows update lifecycle for Hermes-owned embedded Hindsight daemons."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import _scan_venv_blockers as blocker_scan
from hermes_cli import hindsight_update_recovery as recovery
from hermes_cli import main as cli_main
from hermes_cli import update_cmd


class FakeProcess:
    def __init__(
        self,
        pid: int,
        argv: list[str],
        exe: str,
        *,
        created: float,
    ) -> None:
        self.pid = pid
        self._argv = argv
        self._exe = exe
        self._created = created
        self._children: list[FakeProcess] = []
        self._parents: list[FakeProcess] = []
        self.terminated = False
        self.killed = False

    def cmdline(self) -> list[str]:
        return list(self._argv)

    def exe(self) -> str:
        return self._exe

    def create_time(self) -> float:
        return self._created

    def children(self, *, recursive: bool) -> list[FakeProcess]:
        assert recursive is True
        return list(self._children)

    def parents(self) -> list[FakeProcess]:
        return list(self._parents)

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class FakePsutil:
    CONN_LISTEN = "LISTEN"

    def __init__(
        self, processes: list[FakeProcess], listener_pid: int, port: int
    ) -> None:
        self._processes = {process.pid: process for process in processes}
        self.listener_pid = listener_pid
        self.port = port
        self.connection_calls = 0
        self.clear_listener_after = 10_000
        self.keep_processes_alive = False

    def Process(self, pid: int) -> FakeProcess:  # noqa: N802 - psutil API
        if pid not in self._processes:
            raise RuntimeError("missing process")
        return self._processes[pid]

    def net_connections(self, *, kind: str):
        assert kind == "tcp"
        self.connection_calls += 1
        if self.connection_calls >= self.clear_listener_after:
            return []
        return [
            SimpleNamespace(
                status="LISTEN",
                laddr=("127.0.0.1", self.port),
                pid=self.listener_pid,
            )
        ]

    def wait_procs(self, processes, timeout: int):
        assert timeout in {2, 5}
        if self.keep_processes_alive:
            return [], list(processes)
        return list(processes), []


def _fixture(tmp_path: Path, *, env_port: int = 9177):
    project = tmp_path / "repo"
    launcher_exe = project / "venv" / "Scripts" / "pythonw.exe"
    launcher_exe.parent.mkdir(parents=True)
    launcher_exe.write_bytes(b"")

    hermes_root = tmp_path / "hermes"
    config = hermes_root / "hindsight" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"mode": "local_embedded", "profile": "hermes"}),
        encoding="utf-8",
    )
    hindsight_home = tmp_path / ".hindsight"
    profile_env = hindsight_home / "profiles" / "hermes.env"
    profile_env.parent.mkdir(parents=True)
    profile_env.write_text(
        f"HINDSIGHT_API_PORT={env_port}\nHINDSIGHT_API_LLM_API_KEY=secret\n",
        encoding="utf-8",
    )

    argv = [
        str(launcher_exe),
        "-m",
        "hindsight_api.main",
        "--daemon",
        "--idle-timeout",
        "0",
        "--port",
        "9177",
    ]
    launcher = FakeProcess(100, argv, str(launcher_exe), created=10.0)
    worker_argv = [
        str(tmp_path / "uv" / "pythonw.exe"),
        *argv[1:],
    ]
    worker = FakeProcess(200, worker_argv, worker_argv[0], created=11.0)
    launcher._children = [worker]
    worker._parents = [launcher]
    fake_psutil = FakePsutil([launcher, worker], listener_pid=200, port=9177)
    holders = [(100, "pythonw.exe", " ".join(argv))]
    return (
        project,
        hermes_root,
        hindsight_home,
        profile_env,
        launcher,
        worker,
        fake_psutil,
        holders,
    )


def _entries(tmp_path: Path, *, env_port: int = 9177):
    fixture = _fixture(tmp_path, env_port=env_port)
    project, hermes_root, hindsight_home, *_rest, fake_psutil, holders = fixture
    with patch.object(cli_main, "PROJECT_ROOT", project):
        entries = update_cmd._hindsight_daemon_restart_entries(
            holders,
            hindsight_home=hindsight_home,
            hermes_root=hermes_root,
            psutil_module=fake_psutil,
        )
    return fixture, entries


def test_positive_identity_links_venv_launcher_to_loopback_worker(
    tmp_path: Path,
) -> None:
    fixture, entries = _entries(tmp_path)
    _project, _root, _home, profile_env, *_ = fixture

    assert len(entries) == 1
    assert entries[0]["root_pid"] == 100
    assert entries[0]["listener_pid"] == 200
    assert entries[0]["holder_pids"] == [100]
    assert entries[0]["profile"] == "hermes"
    assert entries[0]["port"] == 9177
    assert entries[0]["profile_env"] == str(profile_env)
    # Secret values are never copied into the recovery identity/token.
    assert "secret" not in repr(entries[0])


def test_port_mismatch_or_foreign_listener_fails_closed(tmp_path: Path) -> None:
    _fixture_data, mismatched = _entries(tmp_path / "port-mismatch", env_port=9188)
    assert mismatched == []

    fixture, _entries_ok = _entries(tmp_path / "foreign")
    project, hermes_root, hindsight_home, _env, launcher, worker, fake, holders = (
        fixture
    )
    foreign = FakeProcess(300, ["foreign.exe"], "foreign.exe", created=12.0)
    fake._processes[300] = foreign
    fake.listener_pid = 300
    with patch.object(cli_main, "PROJECT_ROOT", project):
        result = update_cmd._hindsight_daemon_restart_entries(
            holders,
            hindsight_home=hindsight_home,
            hermes_root=hermes_root,
            psutil_module=fake,
        )
    assert result == []
    assert not launcher.terminated and not worker.terminated and not foreign.terminated


def test_duplicate_unconfigured_profile_port_is_ambiguous(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    project, hermes_root, hindsight_home, *_rest, fake, holders = fixture
    (hindsight_home / "profiles" / "other.env").write_text(
        "HINDSIGHT_API_PORT=9177\n", encoding="utf-8"
    )

    with patch.object(cli_main, "PROJECT_ROOT", project):
        entries = update_cmd._hindsight_daemon_restart_entries(
            holders,
            hindsight_home=hindsight_home,
            hermes_root=hermes_root,
            psutil_module=fake,
        )

    assert entries == []


def test_legacy_embedded_config_is_supported(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    project, hermes_root, hindsight_home, *_rest, fake, holders = fixture
    (hermes_root / "hindsight" / "config.json").unlink()
    (hindsight_home / "config.json").write_text(
        json.dumps({"mode": "local_embedded", "profile": "hermes"}),
        encoding="utf-8",
    )

    with patch.object(cli_main, "PROJECT_ROOT", project):
        entries = update_cmd._hindsight_daemon_restart_entries(
            holders,
            hindsight_home=hindsight_home,
            hermes_root=hermes_root,
            psutil_module=fake,
        )

    assert len(entries) == 1
    assert entries[0]["profile"] == "hermes"


def test_stop_revalidates_identity_then_terminates_full_tree(tmp_path: Path) -> None:
    fixture, entries = _entries(tmp_path)
    project, _root, _home, _env, launcher, worker, fake, _holders = fixture
    # Classification consumed call 1; revalidation sees call 2, and the
    # post-stop check sees the listener gone on call 3.
    fake.clear_listener_after = 3
    with patch.object(cli_main, "PROJECT_ROOT", project):
        stopped = update_cmd._stop_hindsight_daemons_for_update(
            entries, psutil_module=fake
        )
    assert stopped == entries
    assert launcher.terminated is True
    assert worker.terminated is True


def test_stop_refuses_reused_launcher_pid(tmp_path: Path) -> None:
    fixture, entries = _entries(tmp_path)
    project, _root, _home, _env, launcher, worker, fake, _holders = fixture
    launcher._created = 99.0
    with patch.object(cli_main, "PROJECT_ROOT", project):
        stopped = update_cmd._stop_hindsight_daemons_for_update(
            entries, psutil_module=fake
        )
    assert stopped == []
    assert launcher.terminated is False
    assert worker.terminated is False


def test_stop_does_not_claim_success_while_processes_survive(tmp_path: Path) -> None:
    fixture, entries = _entries(tmp_path)
    project, _root, _home, _env, launcher, worker, fake, _holders = fixture
    fake.keep_processes_alive = True
    fake.clear_listener_after = 3

    with patch.object(cli_main, "PROJECT_ROOT", project):
        stopped = update_cmd._stop_hindsight_daemons_for_update(
            entries, psutil_module=fake
        )

    assert stopped == []
    assert launcher.terminated and launcher.killed
    assert worker.terminated and worker.killed


def test_relaunch_uses_fresh_venv_helper_and_is_idempotent(tmp_path: Path) -> None:
    fixture, entries = _entries(tmp_path)
    project = fixture[0]
    python = project / "venv" / "Scripts" / "python.exe"
    python.write_bytes(b"")
    token = {"pending": True, "entries": [entries[0], entries[0]]}
    result = SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(cli_main, "_venv_scripts_dir", return_value=python.parent),
        patch.object(update_cmd.subprocess, "run", return_value=result) as run,
        patch("hermes_cli.update_receipt.record_step"),
    ):
        update_cmd._relaunch_stopped_hindsight_daemons(token)
        update_cmd._relaunch_stopped_hindsight_daemons(token)

    run.assert_called_once()
    command = run.call_args.args[0]
    assert command[:3] == [
        str(python),
        "-m",
        "hermes_cli.hindsight_update_recovery",
    ]
    assert command[-4:] == ["--profile", "hermes", "--port", "9177"]
    assert run.call_args.kwargs["timeout"] == 240
    assert token["pending"] is False


def test_interrupted_hindsight_relaunch_stays_armed_for_retry(tmp_path: Path) -> None:
    fixture, entries = _entries(tmp_path)
    project = fixture[0]
    python = project / "venv" / "Scripts" / "python.exe"
    python.write_bytes(b"")
    token = {"pending": True, "entries": [entries[0]]}
    success = SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(cli_main, "_venv_scripts_dir", return_value=python.parent),
        patch.object(
            update_cmd.subprocess,
            "run",
            side_effect=[KeyboardInterrupt(), success],
        ) as run,
        patch("hermes_cli.update_receipt.record_step"),
    ):
        with pytest.raises(KeyboardInterrupt):
            update_cmd._relaunch_stopped_hindsight_daemons(token)
        assert token["pending"] is True
        assert update_cmd._relaunch_stopped_hindsight_daemons(token) is True

    assert run.call_count == 2
    assert token["pending"] is False


def test_desktop_preflight_defers_only_verified_hindsight(monkeypatch, capsys) -> None:
    holder = (
        100,
        "pythonw.exe",
        "pythonw.exe -m hindsight_api.main --daemon --port 9177",
    )
    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace())
    monkeypatch.setattr(cli_main, "_detect_venv_python_processes", lambda: [holder])
    monkeypatch.setattr(blocker_scan, "_updater_owned_hindsight_pids", lambda _m: {100})

    with pytest.raises(SystemExit) as excinfo:
        blocker_scan.main()
    data = json.loads(capsys.readouterr().out)
    assert excinfo.value.code == 0
    assert data["blocked"] is False
    assert data["processes"] == []
    assert data["deferred_hindsight"] == 1


def test_recovery_requires_recorded_port_and_healthy_daemon(monkeypatch) -> None:
    manager = MagicMock()
    manager.ensure_running.return_value = True
    embed_module = types.ModuleType("hindsight_embed")
    embed_module.DaemonEmbedManager = object
    daemon_module = types.ModuleType("hindsight_embed.daemon_embed_manager")
    embed_module.daemon_embed_manager = daemon_module
    profile_module = types.ModuleType("hindsight_embed.profile_manager")
    profile_module.ProfileManager = lambda: SimpleNamespace(
        resolve_profile_paths=lambda _profile: SimpleNamespace(port=9177)
    )
    monkeypatch.setitem(sys.modules, "hindsight_embed", embed_module)
    monkeypatch.setitem(
        sys.modules, "hindsight_embed.daemon_embed_manager", daemon_module
    )
    monkeypatch.setitem(sys.modules, "hindsight_embed.profile_manager", profile_module)
    monkeypatch.setattr(recovery, "_port_is_free", lambda _port: True)
    monkeypatch.setattr(
        recovery, "_no_reclaim_manager", lambda _base, _module, _port: manager
    )
    monkeypatch.setattr(
        recovery, "_listener_belongs_to_launch", lambda _manager, _port: True
    )
    monkeypatch.setattr(recovery, "_health_ok", lambda _port: True)

    assert recovery.recover("hermes", 9177) == 0
    manager.ensure_running.assert_called_once_with({}, "hermes")
    assert recovery.recover("hermes", 9188) == 2


def test_recovery_revalidates_listener_identity_after_health(monkeypatch) -> None:
    manager = MagicMock()
    manager.ensure_running.return_value = True
    embed_module = types.ModuleType("hindsight_embed")
    embed_module.DaemonEmbedManager = object
    daemon_module = types.ModuleType("hindsight_embed.daemon_embed_manager")
    embed_module.daemon_embed_manager = daemon_module
    profile_module = types.ModuleType("hindsight_embed.profile_manager")
    profile_module.ProfileManager = lambda: SimpleNamespace(
        resolve_profile_paths=lambda _profile: SimpleNamespace(port=9177)
    )
    monkeypatch.setitem(sys.modules, "hindsight_embed", embed_module)
    monkeypatch.setitem(
        sys.modules, "hindsight_embed.daemon_embed_manager", daemon_module
    )
    monkeypatch.setitem(sys.modules, "hindsight_embed.profile_manager", profile_module)
    monkeypatch.setattr(recovery, "_port_is_free", lambda _port: True)
    monkeypatch.setattr(
        recovery, "_no_reclaim_manager", lambda _base, _module, _port: manager
    )
    identities = iter([True, False])
    monkeypatch.setattr(
        recovery,
        "_listener_belongs_to_launch",
        lambda _manager, _port: next(identities),
    )
    monkeypatch.setattr(recovery, "_health_ok", lambda _port: True)

    assert recovery.recover("hermes", 9177) == 2
    assert list(identities) == []


def test_recovery_refuses_occupied_port_without_start_or_kill(monkeypatch) -> None:
    kills: list[int] = []

    class DangerousManager:
        @staticmethod
        def _kill_process(pid: int) -> bool:
            kills.append(pid)
            return True

    embed_module = types.ModuleType("hindsight_embed")
    embed_module.DaemonEmbedManager = DangerousManager
    daemon_module = types.ModuleType("hindsight_embed.daemon_embed_manager")
    embed_module.daemon_embed_manager = daemon_module
    profile_module = types.ModuleType("hindsight_embed.profile_manager")
    profile_module.ProfileManager = lambda: SimpleNamespace(
        resolve_profile_paths=lambda _profile: SimpleNamespace(port=9177)
    )
    monkeypatch.setitem(sys.modules, "hindsight_embed", embed_module)
    monkeypatch.setitem(
        sys.modules, "hindsight_embed.daemon_embed_manager", daemon_module
    )
    monkeypatch.setitem(sys.modules, "hindsight_embed.profile_manager", profile_module)
    monkeypatch.setattr(recovery, "_port_is_free", lambda _port: False)
    factory = MagicMock()
    monkeypatch.setattr(recovery, "_no_reclaim_manager", factory)

    assert recovery.recover("hermes", 9177) == 2
    factory.assert_not_called()
    assert kills == []


def test_no_reclaim_manager_tracks_spawn_and_refuses_process_kill(monkeypatch) -> None:
    spawned: list[tuple[tuple, dict]] = []
    killed: list[int] = []

    def fake_popen(*args, **kwargs):
        spawned.append((args, kwargs))
        return SimpleNamespace(pid=4242)

    daemon_module = SimpleNamespace(
        subprocess=SimpleNamespace(Popen=fake_popen),
    )

    class BaseManager:
        def _clear_port(self, _port: int) -> bool:
            raise AssertionError("the recovery subclass must override port clearing")

        @staticmethod
        def _kill_process(pid: int) -> bool:
            killed.append(pid)
            return True

        def _start_daemon_locked(self) -> bool:
            daemon_module.subprocess.Popen(["hindsight", "--daemon"])
            return True

        @staticmethod
        def _find_pid_on_port(_port: int):
            return 4242

        def ensure_running(self, _env, _profile: str) -> bool:
            return self._start_daemon_locked()

    monkeypatch.setattr(recovery, "_port_is_free", lambda port: port == 9177)
    manager = recovery._no_reclaim_manager(BaseManager, daemon_module, 9177)

    assert manager._clear_port(9177) is True
    assert manager._clear_port(9188) is False
    assert manager._start_daemon_locked() is True
    assert manager.update_recovery_launched_pids
    assert daemon_module.subprocess.Popen is fake_popen
    assert len(spawned) == 1
    with pytest.raises(RuntimeError, match="refuses process termination"):
        manager._kill_process(31337)
    assert killed == []


def test_listener_identity_rejects_reused_launch_pid(monkeypatch) -> None:
    created = {4242: 10.0}

    class Process:
        def __init__(self, pid: int):
            self.pid = pid

        def create_time(self) -> float:
            return created[self.pid]

        def parents(self):
            return []

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=Process))
    daemon_module = SimpleNamespace(
        subprocess=SimpleNamespace(Popen=lambda *_a, **_k: SimpleNamespace(pid=4242))
    )

    class BaseManager:
        @staticmethod
        def _clear_port(_port: int) -> bool:
            return True

        @staticmethod
        def _kill_process(_pid: int) -> bool:
            return True

        def _start_daemon_locked(self) -> bool:
            daemon_module.subprocess.Popen(["hindsight", "--daemon"])
            return True

        @staticmethod
        def _find_pid_on_port(_port: int):
            return 4242

        def ensure_running(self, _env, _profile: str) -> bool:
            return self._start_daemon_locked()

    manager = recovery._no_reclaim_manager(BaseManager, daemon_module, 9177)
    assert manager._start_daemon_locked() is True
    assert recovery._listener_belongs_to_launch(manager, 9177) is True

    # The numeric PID is recycled after the recorded launch. A PID-only
    # identity would accept this unrelated listener; the creation timestamp
    # must make the recovery fail closed.
    created[4242] = 99.0
    assert recovery._listener_belongs_to_launch(manager, 9177) is False


def test_recovery_fails_closed_if_port_is_claimed_between_probes(monkeypatch) -> None:
    port_states = iter([True, False])
    spawned: list[object] = []
    killed: list[int] = []

    def fake_popen(*_args, **_kwargs):
        spawned.append(object())
        return SimpleNamespace(pid=4242)

    daemon_module = types.ModuleType("hindsight_embed.daemon_embed_manager")
    daemon_module.subprocess = SimpleNamespace(Popen=fake_popen)

    class BaseManager:
        def _clear_port(self, _port: int) -> bool:
            raise AssertionError("the recovery subclass must override port clearing")

        @staticmethod
        def _kill_process(pid: int) -> bool:
            killed.append(pid)
            return True

        def _start_daemon_locked(self) -> bool:
            daemon_module.subprocess.Popen(["hindsight", "--daemon"])
            return True

        @staticmethod
        def _find_pid_on_port(_port: int):
            return None

        def ensure_running(self, _env, _profile: str) -> bool:
            # Mirrors the real manager's second check at its clear/start
            # boundary. Another process wins the port after recover()'s
            # initial free probe, so no launch may occur.
            if not self._clear_port(9177):
                return False
            return self._start_daemon_locked()

    embed_module = types.ModuleType("hindsight_embed")
    embed_module.DaemonEmbedManager = BaseManager
    embed_module.daemon_embed_manager = daemon_module
    profile_module = types.ModuleType("hindsight_embed.profile_manager")
    profile_module.ProfileManager = lambda: SimpleNamespace(
        resolve_profile_paths=lambda _profile: SimpleNamespace(port=9177)
    )
    monkeypatch.setitem(sys.modules, "hindsight_embed", embed_module)
    monkeypatch.setitem(
        sys.modules, "hindsight_embed.daemon_embed_manager", daemon_module
    )
    monkeypatch.setitem(sys.modules, "hindsight_embed.profile_manager", profile_module)
    monkeypatch.setattr(recovery, "_port_is_free", lambda _port: next(port_states))

    assert recovery.recover("hermes", 9177) == 1
    assert list(port_states) == []
    assert spawned == []
    assert killed == []


def test_guard_stops_hindsight_then_proceeds_and_registers_recovery() -> None:
    class PastGuard(Exception):
        pass

    class RootSentinel:
        def __truediv__(self, _other):
            raise PastGuard

    args = SimpleNamespace(
        gateway=False,
        check=False,
        no_backup=True,
        backup=False,
        yes=True,
        branch=None,
        force=False,
        force_venv=False,
    )
    holder = [
        (100, "pythonw.exe", "pythonw.exe -m hindsight_api.main --daemon --port 9177")
    ]
    entry = {"profile": "hermes", "port": 9177, "holder_pids": [100]}

    with (
        patch.object(cli_main, "_is_windows", return_value=True),
        patch.object(cli_main, "_venv_scripts_dir", return_value=None),
        patch.object(cli_main, "_run_pre_update_backup"),
        patch.object(cli_main, "_pause_windows_gateways_for_update", return_value=None),
        patch.object(cli_main, "_resume_windows_gateways_after_update"),
        patch.object(
            cli_main, "_detect_venv_python_processes", side_effect=[holder, []]
        ),
        patch.object(cli_main, "_leftover_pausable_gateway_pids", return_value=None),
        patch.object(
            cli_main, "_hindsight_daemon_restart_entries", return_value=[entry]
        ),
        patch.object(
            cli_main, "_stop_hindsight_daemons_for_update", return_value=[entry]
        ) as stop,
        patch.object(cli_main, "PROJECT_ROOT", RootSentinel()),
        patch.object(cli_main, "_register_update_exit_recovery") as exit_register,
        patch("atexit.register") as register,
        patch("time.sleep"),
        patch("hermes_cli.update_receipt.record_step"),
    ):
        with pytest.raises(PastGuard):
            cli_main._cmd_update_impl(args, gateway_mode=False)

    stop.assert_called_once_with([entry])
    registered = register.call_args
    assert registered.args[0] is cli_main._relaunch_stopped_hindsight_daemons
    assert registered.args[1]["entries"] == [entry]
    exit_register.assert_called_once_with(
        cli_main._relaunch_stopped_hindsight_daemons,
        registered.args[1],
        transfer_kind="hindsight",
    )
