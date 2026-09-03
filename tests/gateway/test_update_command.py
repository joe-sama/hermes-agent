"""Tests for /update gateway slash command.

Tests both the _handle_update_command handler (spawns update process) and
the _send_update_notification startup hook (sends results after restart).
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/update", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890", thread_id=None):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
        thread_id=thread_id,
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    """Create a bare GatewayRunner without calling __init__."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._update_prompt_pending = {}
    return runner


# ---------------------------------------------------------------------------
# _handle_update_command
# ---------------------------------------------------------------------------


class TestHandleUpdateCommand:
    """Tests for GatewayRunner._handle_update_command."""

    @pytest.mark.asyncio
    async def test_no_git_directory(self, tmp_path):
        """Returns an error when .git does not exist."""
        runner = _make_runner()
        event = _make_event()
        # Point _hermes_home to tmp_path and project_root to a dir without .git
        fake_root = tmp_path / "project"
        fake_root.mkdir()
        with patch("gateway.run._hermes_home", tmp_path), \
             patch("gateway.run.Path") as MockPath:
            # Path(__file__).parent.parent.resolve() -> fake_root
            MockPath.return_value = MagicMock()
            MockPath.__truediv__ = Path.__truediv__
            # Easier: just patch the __file__ resolution in the method
            pass

        # Simpler approach — mock at method level using a wrapper
        runner = _make_runner()

        with patch("gateway.run._hermes_home", tmp_path):
            # The handler does Path(__file__).parent.parent.resolve()
            # We need to make project_root / '.git' not exist.
            # Since Path(__file__) resolves to the real gateway/run.py,
            # project_root will be the real hermes-agent dir (which HAS .git).
            # Patch Path to control this.
            original_path = Path

            class FakePath(type(Path())):
                pass

            # Actually, simplest: just patch the specific file attr.
            # The _handle_update_command handler lives in gateway/slash_commands.py
            # (extracted from run.py in the god-file decomposition); it resolves
            # project_root via Path(__file__).parent.parent, so fake that file.
            fake_file = str(fake_root / "gateway" / "slash_commands.py")
            (fake_root / "gateway").mkdir(parents=True)
            (fake_root / "gateway" / "slash_commands.py").touch()

            with patch("gateway.slash_commands.__file__", fake_file):
                result = await runner._handle_update_command(event)

        assert "Not a git repository" in result


    @pytest.mark.asyncio
    async def test_resolve_hermes_bin_fallback(self):
        """_resolve_hermes_bin falls back to sys.executable argv when which fails."""
        import sys
        from gateway.run import _resolve_hermes_bin

        fake_spec = MagicMock()
        with patch("shutil.which", return_value=None), \
             patch("importlib.util.find_spec", return_value=fake_spec):
            result = _resolve_hermes_bin()

        assert result == [sys.executable, "-m", "hermes_cli.main"]


    @pytest.mark.asyncio
    async def test_writes_pending_marker(self, tmp_path):
        """Writes .update_pending.json with correct platform and chat info."""
        runner = _make_runner()
        event = _make_event(platform=Platform.TELEGRAM, chat_id="99999")
        event.message_id = "m-update"

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        with patch("gateway.run._hermes_home", hermes_home), \
             patch("gateway.run.__file__", fake_file), \
             patch("shutil.which", side_effect=lambda x: "/usr/bin/hermes" if x == "hermes" else "/usr/bin/setsid"), \
             patch("subprocess.Popen"):
            result = await runner._handle_update_command(event)

        pending_path = hermes_home / ".update_pending.json"
        assert pending_path.exists()
        data = json.loads(pending_path.read_text())
        assert data["platform"] == "telegram"
        assert data["chat_id"] == "99999"
        assert data["chat_type"] == "dm"
        assert data["message_id"] == "m-update"
        assert "timestamp" in data
        assert not (hermes_home / ".update_exit_code").exists()


    @pytest.mark.asyncio
    async def test_fallback_when_no_setsid(self, tmp_path):
        """Falls back to start_new_session=True when setsid is not available."""
        runner = _make_runner()
        event = _make_event()

        fake_root = tmp_path / "project"
        fake_root.mkdir()
        (fake_root / ".git").mkdir()
        (fake_root / "gateway").mkdir()
        (fake_root / "gateway" / "run.py").touch()
        fake_file = str(fake_root / "gateway" / "run.py")
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        mock_popen = MagicMock()

        def which_no_setsid(x):
            if x == "hermes":
                return "/usr/bin/hermes"
            if x == "setsid":
                return None
            return None

        with patch("gateway.slash_commands.sys.platform", "linux"), \
             patch("gateway.run._hermes_home", hermes_home), \
             patch("gateway.run.__file__", fake_file), \
             patch("shutil.which", side_effect=which_no_setsid), \
             patch("subprocess.Popen", mock_popen):
            result = await runner._handle_update_command(event)

        # Verify plain bash -c fallback (no nohup, no setsid)
        call_args = mock_popen.call_args[0][0]
        assert call_args[0] == "bash"
        assert "nohup" not in call_args[2]
        assert ".update_exit_code" in call_args[2]
        # start_new_session=True should be in kwargs
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs.get("start_new_session") is True
        assert "Starting Hermes update" in result

    @pytest.mark.asyncio
    async def test_systemd_gateway_moves_complete_update_wrapper_to_scope(
        self, tmp_path, monkeypatch
    ):
        """setsid alone must not leave the rc writer in the service cgroup."""
        runner = _make_runner()
        runner._schedule_update_notification_watch = MagicMock()
        event = _make_event()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        mock_popen = MagicMock()

        def resolve_binary(name):
            return {
                "setsid": "/usr/bin/setsid",
                "systemd-run": "/usr/bin/systemd-run",
            }.get(name)

        monkeypatch.setenv("INVOCATION_ID", "systemd-test-invocation")
        monkeypatch.setattr("gateway.slash_commands.sys.platform", "linux")
        monkeypatch.setattr(
            "gateway.slash_commands._systemd_scope_manager_for_self",
            lambda: "user",
        )
        with patch("gateway.run._hermes_home", hermes_home), patch(
            "gateway.run._resolve_hermes_bin", return_value=["/usr/bin/hermes"]
        ), patch("shutil.which", side_effect=resolve_binary), patch(
            "subprocess.Popen", mock_popen
        ):
            result = await runner._handle_update_command(event)

        argv = mock_popen.call_args.args[0]
        assert argv[:3] == ["/usr/bin/setsid", "bash", "-c"]
        wrapper = argv[3]
        assert "/usr/bin/systemd-run --user --scope --quiet --collect --" in wrapper
        assert "bash -c" in wrapper
        assert "hermes update --gateway" in wrapper
        assert 'exit_tmp="${exit_file}.tmp.$$"' in wrapper
        assert 'scope_rc=$?' in wrapper
        assert '[ ! -s "$exit_file" ]' in wrapper
        assert "Starting Hermes update" in result

    @pytest.mark.asyncio
    async def test_system_service_uses_system_manager_scope(
        self, tmp_path, monkeypatch
    ):
        runner = _make_runner()
        runner._schedule_update_notification_watch = MagicMock()
        event = _make_event()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        mock_popen = MagicMock()

        monkeypatch.setattr(
            "gateway.slash_commands._systemd_scope_manager_for_self",
            lambda: "system",
        )
        monkeypatch.setattr("gateway.slash_commands.sys.platform", "linux")
        # Exercise the privileged system-manager path deterministically.  The
        # non-root system-service fallback is covered by the tests below.
        monkeypatch.setattr(
            "gateway.slash_commands.os.geteuid", lambda: 0, raising=False
        )
        with patch("gateway.run._hermes_home", hermes_home), patch(
            "gateway.run._resolve_hermes_bin", return_value=["/usr/bin/hermes"]
        ), patch(
            "shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        ), patch("subprocess.Popen", mock_popen):
            result = await runner._handle_update_command(event)

        wrapper = mock_popen.call_args.args[0][3]
        assert "/usr/bin/systemd-run --system --scope --quiet --collect --" in wrapper
        assert "systemd-run --user" not in wrapper
        assert "Starting Hermes update" in result

    @pytest.mark.asyncio
    async def test_unknown_systemd_manager_fails_before_update_starts(
        self, tmp_path, monkeypatch
    ):
        runner = _make_runner()
        runner._schedule_update_notification_watch = MagicMock()
        event = _make_event()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        mock_popen = MagicMock()

        monkeypatch.setattr(
            "gateway.slash_commands._systemd_scope_manager_for_self",
            lambda: "unknown",
        )
        monkeypatch.setattr("gateway.slash_commands.sys.platform", "linux")
        with patch("gateway.run._hermes_home", hermes_home), patch(
            "gateway.run._resolve_hermes_bin", return_value=["/usr/bin/hermes"]
        ), patch("shutil.which", return_value="/usr/bin/setsid"), patch(
            "subprocess.Popen", mock_popen
        ):
            result = await runner._handle_update_command(event)

        mock_popen.assert_not_called()
        assert "cannot determine" in result
        assert not (hermes_home / ".update_pending.json").exists()

    @pytest.mark.asyncio
    async def test_second_update_coalesces_without_overwriting_first(
        self, tmp_path
    ):
        runner = _make_runner()
        runner._schedule_update_notification_watch = MagicMock()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        mock_popen = MagicMock()
        first = _make_event(chat_id="first-chat", user_id="first-user")
        second = _make_event(chat_id="second-chat", user_id="second-user")

        with patch("gateway.run._hermes_home", hermes_home), patch(
            "gateway.run._resolve_hermes_bin", return_value=["/usr/bin/hermes"]
        ), patch(
            "shutil.which",
            side_effect=lambda name: "/usr/bin/setsid" if name == "setsid" else None,
        ), patch("subprocess.Popen", mock_popen):
            first_result = await runner._handle_update_command(first)
            original_pending = (
                hermes_home / ".update_pending.json"
            ).read_text(encoding="utf-8")
            second_result = await runner._handle_update_command(second)

        assert "Starting Hermes update" in first_result
        assert "already running" in second_result
        mock_popen.assert_called_once()
        assert (
            hermes_home / ".update_pending.json"
        ).read_text(encoding="utf-8") == original_pending
        assert json.loads(original_pending)["chat_id"] == "first-chat"

    @pytest.mark.asyncio
    async def test_orphaned_pending_ledger_is_quarantined_then_retried(
        self, tmp_path, monkeypatch
    ):
        runner = _make_runner()
        runner._schedule_update_notification_watch = MagicMock()
        old_watcher = MagicMock()
        old_watcher.done.return_value = False
        runner._update_notification_task = old_watcher
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending_path = hermes_home / ".update_pending.json"
        pending_path.write_text(
            json.dumps({"platform": "telegram", "chat_id": "abandoned"}),
            encoding="utf-8",
        )
        (hermes_home / ".update_output.txt").write_text(
            "partial old output", encoding="utf-8"
        )
        monkeypatch.setattr(
            "gateway.slash_commands._UPDATE_LAUNCH_GRACE_SECONDS", -1.0
        )
        mock_popen = MagicMock()

        with patch("gateway.run._hermes_home", hermes_home), patch(
            "gateway.run._resolve_hermes_bin", return_value=["/usr/bin/hermes"]
        ), patch(
            "shutil.which",
            side_effect=lambda name: "/usr/bin/setsid" if name == "setsid" else None,
        ), patch("subprocess.Popen", mock_popen):
            result = await runner._handle_update_command(
                _make_event(chat_id="fresh-chat")
            )

        assert "Starting Hermes update" in result
        old_watcher.cancel.assert_called_once()
        mock_popen.assert_called_once()
        assert json.loads(pending_path.read_text(encoding="utf-8"))["chat_id"] == (
            "fresh-chat"
        )
        quarantined = list(
            (hermes_home / "update-orphans").glob("*/.update_pending.json")
        )
        assert len(quarantined) == 1
        assert json.loads(quarantined[0].read_text(encoding="utf-8"))[
            "chat_id"
        ] == "abandoned"

    @pytest.mark.asyncio
    async def test_nonroot_system_gateway_without_user_manager_refuses_safely(
        self, tmp_path, monkeypatch
    ):
        runner = _make_runner()
        runner._schedule_update_notification_watch = MagicMock()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        mock_popen = MagicMock()
        monkeypatch.setattr("gateway.slash_commands.sys.platform", "linux")
        monkeypatch.setattr(
            "gateway.slash_commands._systemd_scope_manager_for_self",
            lambda: "system",
        )
        monkeypatch.setattr("gateway.slash_commands.os.geteuid", lambda: 1000, raising=False)
        monkeypatch.setattr(
            "gateway.slash_commands._user_systemd_manager_env",
            lambda **_kwargs: None,
        )

        with patch("gateway.run._hermes_home", hermes_home), patch(
            "gateway.run._resolve_hermes_bin", return_value=["/usr/bin/hermes"]
        ), patch("shutil.which", return_value="/usr/bin/setsid"), patch(
            "subprocess.Popen", mock_popen
        ):
            result = await runner._handle_update_command(_make_event())

        mock_popen.assert_not_called()
        assert "sudo hermes update" in result
        assert not (hermes_home / ".update_pending.json").exists()

    @pytest.mark.asyncio
    async def test_nonroot_system_gateway_escapes_through_live_user_manager(
        self, tmp_path, monkeypatch
    ):
        runner = _make_runner()
        runner._schedule_update_notification_watch = MagicMock()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        mock_popen = MagicMock()
        monkeypatch.setattr("gateway.slash_commands.sys.platform", "linux")
        monkeypatch.setattr(
            "gateway.slash_commands._systemd_scope_manager_for_self",
            lambda: "system",
        )
        monkeypatch.setattr("gateway.slash_commands.os.geteuid", lambda: 1000, raising=False)
        monkeypatch.setattr(
            "gateway.slash_commands._user_systemd_manager_env",
            lambda **_kwargs: {"XDG_RUNTIME_DIR": "/run/user/1000"},
        )

        with patch("gateway.run._hermes_home", hermes_home), patch(
            "gateway.run._resolve_hermes_bin", return_value=["/usr/bin/hermes"]
        ), patch(
            "shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        ), patch("subprocess.Popen", mock_popen):
            result = await runner._handle_update_command(_make_event())

        assert "Starting Hermes update" in result
        wrapper = mock_popen.call_args.args[0][3]
        assert "systemd-run --user --scope" in wrapper
        launch_env = mock_popen.call_args.kwargs["env"]
        assert launch_env["XDG_RUNTIME_DIR"] == "/run/user/1000"
        assert launch_env["_HERMES_UPDATE_SYSTEMD_MANAGER"] == "user"


def test_systemd_scope_manager_detects_user_and_system_cgroups(monkeypatch):
    from gateway import slash_commands

    monkeypatch.setattr(slash_commands.sys, "platform", "linux")
    monkeypatch.setenv("INVOCATION_ID", "test-invocation")
    with patch.object(
        slash_commands.Path,
        "read_text",
        return_value="0::/user.slice/user-1000.slice/user@1000.service/app.slice/hermes.service\n",
    ):
        assert slash_commands._systemd_scope_manager_for_self() == "user"
    with patch.object(
        slash_commands.Path,
        "read_text",
        return_value="0::/system.slice/hermes-gateway.service\n",
    ):
        assert slash_commands._systemd_scope_manager_for_self() == "system"


# ---------------------------------------------------------------------------
# Platform allowlist gate
# ---------------------------------------------------------------------------


class TestUpdateCommandPlatformGate:
    """Tests for the platform-allowlist gate at the top of
    ``_handle_update_command``.  Built-in messaging platforms are listed in
    ``_UPDATE_ALLOWED_PLATFORMS``; plugin-migrated platforms (discord,
    mattermost, teams, …) are NOT in the frozenset and rely on the
    registry's ``allow_update_command=True`` fallback.  Programmatic
    interfaces (ACP, API server, webhooks) must be blocked.
    """


    @pytest.mark.asyncio
    async def test_allows_plugin_platform_via_registry_fallback(
        self, monkeypatch, tmp_path
    ):
        """A plugin-migrated platform (DISCORD) is no longer in
        ``_UPDATE_ALLOWED_PLATFORMS`` but must still pass the gate via
        the registry's ``allow_update_command=True`` flag.

        This test is the empirical guarantee that removing DISCORD from
        the hardcoded frozenset does not regress the /update command for
        Discord users.
        """
        from gateway.run import GatewayRunner

        # Precondition: DISCORD is NOT in the hardcoded set anymore.
        assert Platform.DISCORD not in GatewayRunner._UPDATE_ALLOWED_PLATFORMS

        # Make sure the plugin registry is populated so the fallback fires.
        from hermes_cli.plugins import PluginManager
        PluginManager().discover_and_load(force=True)
        from gateway.platform_registry import platform_registry
        discord_entry = platform_registry.get("discord")
        assert discord_entry is not None
        assert discord_entry.allow_update_command is True

        runner = _make_runner()
        runner._schedule_update_notification_watch = MagicMock()
        event = _make_event(platform=Platform.DISCORD)
        monkeypatch.setenv("HERMES_MANAGED", "")
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        with patch("gateway.run._hermes_home", hermes_home), patch(
            "subprocess.Popen"
        ):
            result = await runner._handle_update_command(event)

        # The gate must NOT have rejected us — anything other than the
        # ``platform_not_messaging`` rejection string is acceptable here.
        # Later steps may legitimately return success ("Starting Hermes
        # update…") or fail for environment reasons.
        assert "only available from messaging platforms" not in result


    @pytest.mark.asyncio
    async def test_allows_homeassistant_via_registry_fallback(
        self, monkeypatch, tmp_path
    ):
        """Same as DISCORD/MATTERMOST: HOMEASSISTANT is now plugin-migrated
        (PR #40709) and not in the hardcoded frozenset; the registry must
        keep /update working via ``allow_update_command=True``.
        """
        from gateway.run import GatewayRunner

        assert Platform.HOMEASSISTANT not in GatewayRunner._UPDATE_ALLOWED_PLATFORMS

        from hermes_cli.plugins import PluginManager
        PluginManager().discover_and_load(force=True)
        from gateway.platform_registry import platform_registry
        ha_entry = platform_registry.get("homeassistant")
        assert ha_entry is not None
        assert ha_entry.allow_update_command is True

        runner = _make_runner()
        runner._schedule_update_notification_watch = MagicMock()
        event = _make_event(platform=Platform.HOMEASSISTANT)
        monkeypatch.setenv("HERMES_MANAGED", "")
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        with patch("gateway.run._hermes_home", hermes_home), patch(
            "subprocess.Popen"
        ):
            result = await runner._handle_update_command(event)

        assert "only available from messaging platforms" not in result


# ---------------------------------------------------------------------------
# _send_update_notification
# ---------------------------------------------------------------------------


class TestSendUpdateNotification:
    """Tests for GatewayRunner._send_update_notification."""


    @pytest.mark.asyncio
    async def test_defers_notification_while_update_still_running(self, tmp_path):
        """Returns False and keeps marker files when the update has not exited yet."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path = hermes_home / ".update_pending.json"
        pending_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "67890", "user_id": "12345",
        }))
        (hermes_home / ".update_output.txt").write_text("still running")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is False
        mock_adapter.send.assert_not_called()
        assert pending_path.exists()

    @pytest.mark.asyncio
    async def test_recovers_from_claimed_pending_file(self, tmp_path):
        """A claimed pending file from a crashed notifier is still deliverable."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        claimed_path = hermes_home / ".update_pending.claimed.json"
        claimed_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "67890", "user_id": "12345",
        }))
        (hermes_home / ".update_output.txt").write_text("done")
        (hermes_home / ".update_exit_code").write_text("0")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        assert result is True
        mock_adapter.send.assert_called_once()
        assert not claimed_path.exists()

    @pytest.mark.asyncio
    async def test_partial_exit_marker_is_deferred_then_delivered_once(self, tmp_path):
        """A created-but-not-written marker is not a terminal failure."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending_path = hermes_home / ".update_pending.json"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(
            json.dumps(
                {"platform": "telegram", "chat_id": "67890", "user_id": "12345"}
            ),
            encoding="utf-8",
        )
        exit_code_path.write_text("", encoding="utf-8")
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            first = await runner._send_update_notification()

        assert first is False
        mock_adapter.send.assert_not_called()
        assert pending_path.exists()
        assert exit_code_path.exists()

        exit_code_path.write_text("0", encoding="utf-8")
        with patch("gateway.run._hermes_home", hermes_home):
            second = await runner._send_update_notification()

        assert second is True
        mock_adapter.send.assert_called_once()
        assert "finished" in mock_adapter.send.call_args[0][1].lower()
        assert not pending_path.exists()
        assert not exit_code_path.exists()

    @pytest.mark.asyncio
    async def test_unresolved_adapter_deadline_preserves_producer_owned_marker(
        self, tmp_path
    ):
        """A watcher deadline cannot manufacture an updater exit status."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending_path = hermes_home / ".update_pending.json"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(
            json.dumps(
                {"platform": "telegram", "chat_id": "67890", "user_id": "12345"}
            ),
            encoding="utf-8",
        )
        exit_code_path.write_text("", encoding="utf-8")
        runner.adapters = {}

        runner._send_update_notification = AsyncMock()
        with patch("gateway.run._hermes_home", hermes_home):
            await runner._watch_update_progress(poll_interval=0, timeout=0)

        runner._send_update_notification.assert_not_awaited()
        assert exit_code_path.read_text(encoding="utf-8") == ""
        assert pending_path.exists()

    @pytest.mark.asyncio
    async def test_sends_notification_with_output(self, tmp_path):
        """Sends update output to the correct platform and chat."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        # Write pending marker
        pending = {
            "platform": "telegram",
            "chat_id": "67890",
            "user_id": "12345",
            "timestamp": "2026-03-04T21:00:00",
        }
        (hermes_home / ".update_pending.json").write_text(
            json.dumps(pending), encoding="utf-8"
        )
        (hermes_home / ".update_output.txt").write_text(
            "→ Found 3 new commit(s)\n✓ Code updated!\n✓ Update complete!",
            encoding="utf-8",
        )
        (hermes_home / ".update_exit_code").write_text("0", encoding="utf-8")

        # Mock the adapter
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._send_update_notification()

        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        assert call_args[0][0] == "67890"  # chat_id
        assert "Update complete" in call_args[0][1] or "update finished" in call_args[0][1].lower()


    @pytest.mark.asyncio
    async def test_send_failure_preserves_evidence_then_retry_cleans_up(self, tmp_path):
        """A transient final-send error keeps the exact result for retry."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(json.dumps({
            "platform": "telegram", "chat_id": "111", "user_id": "222",
        }), encoding="utf-8")
        output_path.write_text("✓ Done", encoding="utf-8")
        exit_code_path.write_text("0", encoding="utf-8")

        # Adapter send raises
        mock_adapter = AsyncMock()
        mock_adapter.send.side_effect = [RuntimeError("network error"), None]
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            first = await runner._send_update_notification()

        assert first is False
        assert pending_path.exists()
        assert not (hermes_home / ".update_pending.claimed.json").exists()
        assert output_path.exists()
        assert exit_code_path.exists()

        with patch("gateway.run._hermes_home", hermes_home):
            second = await runner._send_update_notification()

        assert second is True
        assert mock_adapter.send.await_count == 2
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()

    @pytest.mark.asyncio
    async def test_cancellation_while_sending_preserves_completion_evidence(
        self, tmp_path
    ):
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(
            json.dumps({"platform": "telegram", "chat_id": "111"}),
            encoding="utf-8",
        )
        output_path.write_text("done", encoding="utf-8")
        exit_code_path.write_text("0", encoding="utf-8")
        send_started = asyncio.Event()
        never_finish = asyncio.Event()

        async def blocked_send(*_args, **_kwargs):
            send_started.set()
            await never_finish.wait()

        adapter = AsyncMock()
        adapter.send.side_effect = blocked_send
        runner.adapters = {Platform.TELEGRAM: adapter}
        with patch("gateway.run._hermes_home", hermes_home):
            task = asyncio.create_task(runner._send_update_notification())
            await send_started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert pending_path.exists()
        assert not (hermes_home / ".update_pending.claimed.json").exists()
        assert output_path.exists()
        assert exit_code_path.exists()

    @pytest.mark.asyncio
    async def test_metadata_failure_preserves_then_retry_delivers(self, tmp_path):
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(
            json.dumps({"platform": "telegram", "chat_id": "111"}),
            encoding="utf-8",
        )
        output_path.write_text("done", encoding="utf-8")
        exit_code_path.write_text("0", encoding="utf-8")
        adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: adapter}
        runner._thread_metadata_for_target = MagicMock(
            side_effect=[RuntimeError("temporary metadata failure"), None]
        )

        with patch("gateway.run._hermes_home", hermes_home):
            first = await runner._send_update_notification()
            second = await runner._send_update_notification()

        assert first is False
        assert second is True
        assert runner._thread_metadata_for_target.call_count == 2
        adapter.send.assert_awaited_once()
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()


    @pytest.mark.asyncio
    async def test_no_adapter_for_platform_preserves_markers(self, tmp_path):
        """A finished update whose platform is offline keeps its markers.

        When the target platform's adapter has not reconnected yet, dropping
        the completion markers would silently lose the notification. Instead the
        call defers (returns False) and leaves every marker on disk so a later
        retry can deliver once the platform is back.
        """
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending = {"platform": "discord", "chat_id": "111", "user_id": "222"}
        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(json.dumps(pending))
        output_path.write_text("Done")
        exit_code_path.write_text("0")

        # Only telegram adapter available, but pending says discord
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            result = await runner._send_update_notification()

        # No send (wrong platform offline) and the result is deferred.
        assert result is False
        mock_adapter.send.assert_not_called()
        # Markers are preserved for a later retry — NOT cleaned up.
        assert pending_path.exists()
        assert output_path.exists()
        assert exit_code_path.exists()
        # The marker stays in its canonical pending location (claim restored).
        assert not (hermes_home / ".update_pending.claimed.json").exists()

    @pytest.mark.asyncio
    async def test_deferred_notification_delivers_after_reconnect(self, tmp_path):
        """A deferred completion is delivered once the platform reconnects.

        Regression for the late-reconnect /update bug: the update finishes while
        the target platform is offline, the markers survive the deferral, and
        the next call (after the adapter is registered) delivers the result and
        cleans up — exactly once.
        """
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending = {"platform": "discord", "chat_id": "111", "user_id": "222"}
        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(json.dumps(pending), encoding="utf-8")
        output_path.write_text("✓ Update complete!", encoding="utf-8")
        exit_code_path.write_text("0", encoding="utf-8")

        # First pass: target platform (discord) is still offline → defer.
        with patch("gateway.run._hermes_home", hermes_home):
            first = await runner._send_update_notification()

        assert first is False
        assert pending_path.exists()

        # Platform reconnects: the reconnect watcher adds the adapter back.
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.DISCORD: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            second = await runner._send_update_notification()

        assert second is True
        mock_adapter.send.assert_called_once()
        sent_text = mock_adapter.send.call_args[0][1]
        assert "Update complete" in sent_text
        # Now everything is cleaned up — no duplicate deliveries possible.
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()
        assert not (hermes_home / ".update_pending.claimed.json").exists()

    @pytest.mark.asyncio
    async def test_completion_notification_tolerates_invalid_utf8_output(self, tmp_path):
        """Completion-only update notifications must not crash on bad bytes."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        pending = {"platform": "discord", "chat_id": "111", "user_id": "222"}
        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(json.dumps(pending))
        output_path.write_bytes(b"ok before\ninvalid byte: \x96\ncontinued after\n")
        exit_code_path.write_text("0")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.DISCORD: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            delivered = await runner._send_update_notification()

        assert delivered is True
        mock_adapter.send.assert_called_once()
        sent_text = mock_adapter.send.call_args[0][1]
        assert "ok before" in sent_text
        assert "invalid byte" in sent_text
        assert "continued after" in sent_text
        assert "Hermes update finished" in sent_text
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()


# ---------------------------------------------------------------------------
# /update in help and known_commands
# ---------------------------------------------------------------------------


class TestUpdateInHelp:
    """Verify /update appears in help text and known commands set."""


    def test_update_is_known_command(self):
        """/update dispatches through the gateway's plain-command handler table.

        (Was an inspect.getsource() check for the literal '"update"' in
        _handle_message — a banned source-reading test. The if-chain was
        replaced by _gateway_plain_command_handlers(), so assert the real
        dispatch contract: the table maps "update" to the update handler.)
        """
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        handlers = runner._gateway_plain_command_handlers()
        assert handlers.get("update") == runner._handle_update_command

class TestWatchUpdateProgress:
    @pytest.mark.asyncio
    async def test_deadline_race_never_overwrites_real_updater_result(
        self, tmp_path
    ):
        """A real rc published after the watcher's read remains authoritative."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending_path = hermes_home / ".update_pending.json"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(
            json.dumps(
                {"platform": "telegram", "chat_id": "67890", "user_id": "12345"}
            ),
            encoding="utf-8",
        )
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        def publish_after_incomplete_read(path):
            path.write_text("0", encoding="utf-8")
            return None

        with patch("gateway.run._hermes_home", hermes_home), patch(
            "gateway.run._read_terminal_update_exit_code",
            side_effect=publish_after_incomplete_read,
        ):
            await runner._watch_update_progress(poll_interval=0, timeout=0)

        assert exit_code_path.read_text(encoding="utf-8") == "0"
        assert pending_path.exists()
        mock_adapter.send.assert_not_awaited()

        with patch("gateway.run._hermes_home", hermes_home):
            delivered = await runner._send_update_notification()

        assert delivered is True
        assert "finished" in mock_adapter.send.await_args.args[1].lower()
        assert not pending_path.exists()
        assert not exit_code_path.exists()

    @pytest.mark.asyncio
    async def test_long_update_survives_watch_interval_then_delivers(self, tmp_path):
        """Elapsed watch time is advisory; a later real rc is still delivered."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending_path = hermes_home / ".update_pending.json"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(
            json.dumps(
                {"platform": "telegram", "chat_id": "67890", "user_id": "12345"}
            ),
            encoding="utf-8",
        )
        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            watcher = asyncio.create_task(
                runner._watch_update_progress(
                    poll_interval=0.001,
                    stream_interval=0.001,
                    timeout=0.005,
                )
            )
            await asyncio.sleep(0.02)
            assert not watcher.done()
            assert pending_path.exists()
            assert not exit_code_path.exists()
            exit_code_path.write_text("0", encoding="utf-8")
            await asyncio.wait_for(watcher, timeout=1)

        assert mock_adapter.send.await_count == 1
        assert "finished" in mock_adapter.send.await_args.args[1].lower()
        assert not pending_path.exists()
        assert not exit_code_path.exists()

    @pytest.mark.asyncio
    async def test_streaming_final_send_failure_retries_before_cleanup(self, tmp_path):
        """Streaming completion commits cleanup only after final delivery."""
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending_path = hermes_home / ".update_pending.json"
        output_path = hermes_home / ".update_output.txt"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(
            json.dumps(
                {"platform": "telegram", "chat_id": "67890", "user_id": "12345"}
            ),
            encoding="utf-8",
        )
        output_path.write_text("", encoding="utf-8")
        exit_code_path.write_text("0", encoding="utf-8")
        attempts = 0

        async def flaky_send(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            assert pending_path.exists()
            assert output_path.exists()
            assert exit_code_path.read_text(encoding="utf-8") == "0"
            if attempts == 1:
                raise RuntimeError("platform reconnecting")

        mock_adapter = AsyncMock()
        mock_adapter.send.side_effect = flaky_send
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._watch_update_progress(poll_interval=0, timeout=1)

        assert attempts == 2
        assert not pending_path.exists()
        assert not output_path.exists()
        assert not exit_code_path.exists()

    @pytest.mark.asyncio
    async def test_streaming_retry_uses_reconnected_adapter(self, tmp_path):
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        pending_path = hermes_home / ".update_pending.json"
        exit_code_path = hermes_home / ".update_exit_code"
        pending_path.write_text(
            json.dumps(
                {"platform": "telegram", "chat_id": "67890", "user_id": "12345"}
            ),
            encoding="utf-8",
        )
        exit_code_path.write_text("0", encoding="utf-8")
        replacement = AsyncMock()
        stale = AsyncMock()

        async def disconnect_then_replace(*_args, **_kwargs):
            runner.adapters[Platform.TELEGRAM] = replacement
            raise RuntimeError("old transport disconnected")

        stale.send.side_effect = disconnect_then_replace
        runner.adapters = {Platform.TELEGRAM: stale}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._watch_update_progress(poll_interval=0, timeout=1)

        stale.send.assert_awaited_once()
        replacement.send.assert_awaited_once()
        assert not pending_path.exists()
        assert not exit_code_path.exists()

    @pytest.mark.asyncio
    async def test_invalid_utf8_update_output_does_not_crash_watcher(self, tmp_path):
        runner = _make_runner()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        (hermes_home / ".update_pending.json").write_text(json.dumps({
            "platform": "telegram",
            "chat_id": "67890",
            "user_id": "12345",
        }))
        (hermes_home / ".update_output.txt").write_bytes(
            b"ok before\n\xe2\x9c invalid-continuation: \x96\ncontinued after\n"
        )
        (hermes_home / ".update_exit_code").write_text("0")

        mock_adapter = AsyncMock()
        runner.adapters = {Platform.TELEGRAM: mock_adapter}

        with patch("gateway.run._hermes_home", hermes_home):
            await runner._watch_update_progress(poll_interval=0.01, stream_interval=0.01, timeout=1.0)

        sent = "\n".join(call.args[1] for call in mock_adapter.send.call_args_list)
        assert "ok before" in sent
        assert "continued after" in sent
        assert "Hermes update finished" in sent
        assert not (hermes_home / ".update_pending.json").exists()
