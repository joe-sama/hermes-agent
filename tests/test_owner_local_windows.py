"""Native-Windows contracts for Yousef's owner-local startup stack.

The owner profile deliberately keeps Hindsight in a separate venv because
Hermes uses MCP 2 while Hindsight 0.9.1's FastMCP server still requires MCP
below 2. These tests exercise the PowerShell entry points as subprocesses;
they do not inspect source strings or touch the operator's live config.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from plugins.memory.hindsight import _validate_windows_file_owner_only


pytestmark = pytest.mark.windows_only

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
_POWERSHELL = (
    Path(os.environ["SystemRoot"])
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe"
)
_POWERSHELL_ENV = os.environ.copy()
_POWERSHELL_ENV.setdefault(
    "SystemDrive", Path(os.environ["SystemRoot"]).anchor.rstrip("\\")
)


def _run_script(
    script: str, *args: str, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(_POWERSHELL),
            "-NoProfile",
            "-NoLogo",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(_SCRIPTS / script),
            *map(str, args),
        ],
        cwd=_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=_POWERSHELL_ENV,
        timeout=timeout,
        check=False,
    )


def _powershell_quote(value: str | Path) -> str:
    return "'" + os.fspath(value).replace("'", "''") + "'"


def _assert_private_inheritable_directory(path: Path) -> None:
    import win32con
    import win32file
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    control, _revision = descriptor.GetSecurityDescriptorControl()
    assert control & win32security.SE_DACL_PROTECTED
    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None and dacl.GetAceCount() == 1
    header, access_mask, ace_sid = dacl.GetAce(0)
    assert header[0] == win32security.ACCESS_ALLOWED_ACE_TYPE
    assert header[1] & win32con.OBJECT_INHERIT_ACE
    assert header[1] & win32con.CONTAINER_INHERIT_ACE
    assert access_mask & win32file.FILE_ALL_ACCESS == win32file.FILE_ALL_ACCESS

    token = win32security.OpenProcessToken(
        __import__("win32api").GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        current_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[
            0
        ]
    finally:
        token.Close()
    assert win32security.ConvertSidToStringSid(ace_sid) == (
        win32security.ConvertSidToStringSid(current_sid)
    )


def _assert_only_current_user_can_read_file(path: Path) -> None:
    import win32con
    import win32file
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None and dacl.GetAceCount() == 1
    header, access_mask, ace_sid = dacl.GetAce(0)
    assert header[0] == win32security.ACCESS_ALLOWED_ACE_TYPE
    assert access_mask & win32file.FILE_ALL_ACCESS == win32file.FILE_ALL_ACCESS

    import win32api

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        current_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[
            0
        ]
    finally:
        token.Close()
    assert win32security.ConvertSidToStringSid(ace_sid) == (
        win32security.ConvertSidToStringSid(current_sid)
    )


class _HealthHandler(BaseHTTPRequestHandler):
    server_version = "OwnerHealthTest/1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/health":
            self.send_error(404)
            return
        expected_auth = getattr(self.server, "expected_auth", None)
        if expected_auth and self.headers.get("Authorization") != expected_auth:
            self.send_error(401)
            return
        payload = json.dumps(self.server.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@contextmanager
def _health_server(payload: dict, *, expected_auth: str | None = None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    server.payload = payload
    server.expected_auth = expected_auth
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_configure_owner_uses_external_runtime_and_private_data_dirs(tmp_path: Path):
    user_home = tmp_path / "user"
    hindsight_home = user_home / ".hindsight"
    hermes_home = tmp_path / "hermes"
    model_state = tmp_path / "model-state"
    model_runtime = model_state / "llama.cpp" / "build"
    model_runtime.mkdir(parents=True)
    (model_state / "llama.cpp" / "server-api-key.txt").write_text(
        "owner-test-key", encoding="utf-8"
    )
    hermes_home.mkdir(parents=True)
    (hermes_home / "config.yaml").write_text(
        """# keep this owner comment
_config_version: 987
plugins:
  enabled:
    - keep-plugin
  keep-plugin:
    nullable: null
providers:
  unrelated-provider:
    api: https://preserve.example/v1
  local-qwen38:
    future_provider_key: keep-provider-key
agent:
  max_turns: 123
  reasoning_effort: low
  future_agent_key: keep-agent-key
approvals:
  deny:
    - old-owner-value
display:
  future_display_key: keep-display-key
future_root:
  nested: keep-root-key
""",
        encoding="utf-8",
    )
    existing_profile_log = hindsight_home / "profiles" / "existing.log"
    existing_profile_log.parent.mkdir(parents=True)
    existing_profile_log.write_text("profile log survives", encoding="utf-8")
    existing_db_file = user_home / ".pg0" / "instances" / "existing" / "db.bin"
    existing_db_file.parent.mkdir(parents=True)
    existing_db_file.write_bytes(b"database survives")

    result = _run_script(
        "configure-owner-local.ps1",
        "-HermesHome",
        str(hermes_home),
        "-HermesPython",
        os.fspath(Path(os.sys.executable)),
        "-RuntimeRoot",
        str(model_runtime),
        "-HindsightRuntimeRoot",
        str(tmp_path / "hindsight-runtime"),
        "-HindsightHome",
        str(hindsight_home),
        "-HindsightProfile",
        "hermes",
        "-HindsightPort",
        "19177",
        "-SkipHindsightInstall",
        "-SkipCuaTelemetry",
        "-SkipStartupTask",
    )
    assert result.returncode == 0, result.stderr or result.stdout

    hermes_config_text = (hermes_home / "config.yaml").read_text(encoding="utf-8")
    hermes_config = yaml.safe_load(hermes_config_text)
    assert hermes_config["_config_version"] == 987
    assert hermes_config["plugins"] == {
        "enabled": ["keep-plugin"],
        "keep-plugin": {"nullable": None},
    }
    assert hermes_config["providers"]["unrelated-provider"] == {
        "api": "https://preserve.example/v1"
    }
    assert (
        hermes_config["providers"]["local-qwen38"]["future_provider_key"]
        == "keep-provider-key"
    )
    assert hermes_config["agent"]["future_agent_key"] == "keep-agent-key"
    assert hermes_config["display"]["future_display_key"] == "keep-display-key"
    assert hermes_config["future_root"] == {"nested": "keep-root-key"}
    assert hermes_config["agent"]["max_turns"] is None
    assert hermes_config["agent"]["reasoning_effort"] == "xhigh"
    assert hermes_config["model"]["max_tokens"] == 4096
    assert hermes_config["model"]["reasoning_echo"] is False
    assert hermes_config["compression"]["threshold"] == 0.50
    assert hermes_config["compression"]["threshold_tokens"] == 32000
    assert hermes_config["memory"]["nudge_interval"] == 10
    assert (
        hermes_config["providers"]["local-qwen38"]["extra_body"][
            "reasoning_effort"
        ]
        == "xhigh"
    )
    assert (
        hermes_config["providers"]["local-qwen38"]["extra_body"]
        ["chat_template_kwargs"]["preserve_thinking"]
        is False
    )
    assert hermes_config["auxiliary"]["compression"]["reasoning_effort"] == "low"
    assert (
        hermes_config["auxiliary"]["compression"]["extra_body"]
        ["reasoning_effort"]
        == "low"
    )
    assert (
        hermes_config["providers"]["local-qwen38"]["extra_body"][
            "chat_template_kwargs"
        ]["reasoning_effort"]
        == "xhigh"
    )
    assert (
        hermes_config["auxiliary"]["background_review"]["reasoning_effort"]
        == "xhigh"
    )
    assert hermes_config["delegation"]["reasoning_effort"] == "xhigh"
    assert hermes_config["approvals"]["deny"] == []
    assert "# keep this owner comment" in hermes_config_text

    config = json.loads((hermes_home / "hindsight" / "config.json").read_text())
    assert config["mode"] == "local_external"
    assert config["api_url"] == "http://127.0.0.1:19177"
    assert config["profile"] == "hermes"

    hermes_env = (hermes_home / ".env").read_text(encoding="utf-8")
    assert "LLAMA_API_KEY=owner-test-key" in hermes_env
    assert "HINDSIGHT_LLM_API_KEY=" not in hermes_env
    assert "HINDSIGHT_TIMEOUT=" not in hermes_env
    assert "HINDSIGHT_IDLE_TIMEOUT=" not in hermes_env

    profile_env = hindsight_home / "profiles" / "hermes.env"
    profile_text = profile_env.read_text(encoding="utf-8")
    assert "HINDSIGHT_API_LLM_API_KEY=owner-test-key" in profile_text
    assert "HINDSIGHT_API_PORT=19177" in profile_text
    assert "HINDSIGHT_API_HOST=127.0.0.1" in profile_text
    assert "HINDSIGHT_API_RETAIN_LLM_REASONING_EFFORT=low" in profile_text
    assert "HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS=4096" in profile_text
    assert "HINDSIGHT_API_RETAIN_LLM_TIMEOUT=90" in profile_text
    assert "HINDSIGHT_API_RETAIN_LLM_MAX_RETRIES=0" in profile_text
    assert "HINDSIGHT_API_RETAIN_WALL_TIMEOUT=120" in profile_text

    for private_file in (
        hermes_home / ".env",
        hermes_home / "hindsight" / "config.json",
        profile_env,
    ):
        _validate_windows_file_owner_only(private_file)
    _assert_private_inheritable_directory(hindsight_home / "profiles")
    _assert_private_inheritable_directory(user_home / ".pg0" / "instances")
    assert existing_profile_log.read_text(encoding="utf-8") == "profile log survives"
    assert existing_db_file.read_bytes() == b"database survives"
    _assert_only_current_user_can_read_file(existing_profile_log)
    _assert_only_current_user_can_read_file(existing_db_file)


@pytest.mark.parametrize("invalid_config", [b"plugins: [\n", b"- not\n- a-mapping\n"])
def test_owner_config_merge_refuses_to_replace_invalid_config(
    tmp_path: Path, invalid_config: bytes
):
    config_path = tmp_path / "config.yaml"
    overlay_path = tmp_path / "overlay.yaml"
    config_path.write_bytes(invalid_config)
    overlay_path.write_text("agent:\n  reasoning_effort: xhigh\n", encoding="utf-8")

    result = subprocess.run(
        [
            os.sys.executable,
            "-I",
            str(_SCRIPTS / "merge-owner-config.py"),
            str(config_path),
            str(overlay_path),
        ],
        cwd=_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert config_path.read_bytes() == invalid_config


def test_configure_fails_closed_when_gateway_task_cannot_be_removed(tmp_path: Path):
    user_home = tmp_path / "user"
    hindsight_home = user_home / ".hindsight"
    hermes_home = tmp_path / "hermes"
    startup_directory = tmp_path / "startup"
    startup_directory.mkdir(parents=True)
    existing_wrapper = startup_directory / "Hermes_Gateway.vbs"
    existing_wrapper.write_text("existing gated wrapper", encoding="ascii")

    model_state = tmp_path / "model-state"
    model_runtime = model_state / "llama.cpp" / "build"
    model_runtime.mkdir(parents=True)
    (model_state / "llama.cpp" / "server-api-key.txt").write_text(
        "owner-test-key", encoding="utf-8"
    )

    command = (
        "function Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName); "
        "if ($TaskName -eq 'Hermes_Gateway') { "
        "[pscustomobject]@{ TaskName = $TaskName } } }; "
        "function Unregister-ScheduledTask { "
        "[CmdletBinding(SupportsShouldProcess=$true)] param([string]$TaskName); "
        "if ($TaskName -ne 'Hermes_Gateway') { throw 'wrong task requested' }; "
        "throw 'simulated access denied' }; "
        f"& {_powershell_quote(_SCRIPTS / 'configure-owner-local.ps1')} "
        f"-HermesHome {_powershell_quote(hermes_home)} "
        f"-HermesPython {_powershell_quote(os.sys.executable)} "
        f"-RuntimeRoot {_powershell_quote(model_runtime)} "
        f"-HindsightRuntimeRoot {_powershell_quote(tmp_path / 'hindsight-runtime')} "
        f"-HindsightHome {_powershell_quote(hindsight_home)} "
        f"-StartupDirectory {_powershell_quote(startup_directory)} "
        "-SkipHindsightInstall -SkipCuaTelemetry"
    )
    result = subprocess.run(
        [str(_POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=_POWERSHELL_ENV,
        timeout=60,
        check=False,
    )

    assert result.returncode != 0
    combined_output = result.stderr + result.stdout
    assert (
        "Could not remove the direct Hermes_Gateway Scheduled Task" in combined_output
    )
    assert "simulated access denied" in combined_output
    assert existing_wrapper.read_text(encoding="ascii") == "existing gated wrapper"


def test_hindsight_start_accepts_current_flat_database_health(tmp_path: Path):
    # The fake health endpoint is served by this pytest process. Point the
    # configured runtime at its real venv so the listener-ownership proof is
    # exercised instead of bypassed.
    runtime = Path(os.sys.executable).resolve().parent.parent
    hindsight_home = tmp_path / "user" / ".hindsight"
    profile = hindsight_home / "profiles" / "hermes.env"
    profile.parent.mkdir(parents=True)

    with _health_server({"status": "healthy", "database": "connected"}) as port:
        profile.write_text(f"HINDSIGHT_API_PORT={port}\n", encoding="utf-8")
        result = _run_script(
            "start-owner-hindsight.ps1",
            "-RuntimeRoot",
            str(runtime),
            "-HindsightHome",
            str(hindsight_home),
            "-Profile",
            "hermes",
            "-Port",
            str(port),
            "-StartupTimeoutSeconds",
            "30",
        )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "Isolated Hindsight ready" in result.stdout
    _validate_windows_file_owner_only(profile)


def test_hindsight_start_rejects_healthy_daemon_from_another_runtime(
    tmp_path: Path,
):
    runtime = tmp_path / "different-hindsight-runtime"
    python = runtime / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    hindsight_home = tmp_path / "user" / ".hindsight"
    profile = hindsight_home / "profiles" / "hermes.env"
    profile.parent.mkdir(parents=True)

    with _health_server({"status": "healthy", "database": "connected"}) as port:
        profile.write_text(f"HINDSIGHT_API_PORT={port}\n", encoding="utf-8")
        result = _run_script(
            "start-owner-hindsight.ps1",
            "-RuntimeRoot",
            str(runtime),
            "-HindsightHome",
            str(hindsight_home),
            "-Profile",
            "hermes",
            "-Port",
            str(port),
            "-StartupTimeoutSeconds",
            "30",
        )

    assert result.returncode != 0
    assert "does not belong to isolated runtime" in (result.stderr + result.stdout)


def test_gateway_probe_waits_for_authenticated_model_and_memory(tmp_path: Path):
    model_state = tmp_path / "model-state"
    runtime = model_state / "llama.cpp" / "build"
    runtime.mkdir(parents=True)
    key = "owner-gateway-test-key"
    (model_state / "llama.cpp" / "server-api-key.txt").write_text(key, encoding="utf-8")

    with ExitStack() as stack:
        model_port = stack.enter_context(
            _health_server({"status": "ok"}, expected_auth=f"Bearer {key}")
        )
        memory_port = stack.enter_context(
            _health_server({"status": "healthy", "database": "connected"})
        )
        result = _run_script(
            "start-owner-gateway.ps1",
            "-RuntimeRoot",
            str(runtime),
            "-ModelPort",
            str(model_port),
            "-HindsightPort",
            str(memory_port),
            "-StartupTimeoutSeconds",
            "30",
            "-ProbeOnly",
        )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "dependencies are ready" in result.stdout


def test_owner_powershell_entrypoints_parse_under_windows_powershell_51():
    paths = [
        _SCRIPTS / "configure-owner-local.ps1",
        _SCRIPTS / "start-owner-local-ai.ps1",
        _SCRIPTS / "start-owner-hindsight.ps1",
        _SCRIPTS / "start-owner-gateway.ps1",
    ]
    quoted = ",".join("'" + str(path).replace("'", "''") + "'" for path in paths)
    command = (
        "$failed=$false; foreach($path in @(" + quoted + ")) { "
        "$tokens=$null; $errors=$null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors); "
        "if($errors.Count -gt 0){$errors | ForEach-Object { Write-Error $_ }; $failed=$true} }; "
        "if($failed){exit 1}"
    )
    result = subprocess.run(
        [str(_POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=_POWERSHELL_ENV,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
