"""Native-Windows contracts for Yousef's owner-local startup stack.

The owner profile deliberately keeps Hindsight in a separate venv because
Hermes uses MCP 2 while Hindsight 0.9.1's FastMCP server still requires MCP
below 2. These tests exercise the PowerShell entry points as subprocesses;
they do not inspect source strings or touch the operator's live config.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from hermes_cli.config import DEFAULT_CONFIG
from plugins.memory.hindsight import _validate_windows_file_owner_only


pytestmark = pytest.mark.windows_only

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
_SYSTEM_ROOT = Path(os.environ.get("SystemRoot", r"C:\Windows"))
_POWERSHELL = (
    _SYSTEM_ROOT
    / "System32"
    / "WindowsPowerShell"
    / "v1.0"
    / "powershell.exe"
)
_POWERSHELL_ENV = os.environ.copy()
_POWERSHELL_ENV.setdefault(
    "SystemDrive", _SYSTEM_ROOT.drive or "C:"
)


def _run_script(
    script: str,
    *args: str,
    timeout: int = 60,
    env: dict[str, str] | None = None,
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
        env=_POWERSHELL_ENV if env is None else env,
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


def _seed_path_with_explicit_system_access(path: Path) -> None:
    """Give a fixture the extra explicit ACE seen on some Windows hosts."""
    import win32file
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32file.FILE_GENERIC_READ,
        win32security.CreateWellKnownSid(win32security.WinLocalSystemSid),
    )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )


def _dacl_sddl(path: Path) -> str:
    """Return a stable DACL snapshot for out-of-scope mutation checks."""
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
        descriptor,
        win32security.SDDL_REVISION_1,
        win32security.DACL_SECURITY_INFORMATION,
    )


def _create_directory_junction(path: Path, target: Path) -> None:
    command = (
        "New-Item -ItemType Junction -Path "
        + _powershell_quote(path)
        + " -Target "
        + _powershell_quote(target)
        + " -ErrorAction Stop | Out-Null"
    )
    result = subprocess.run(
        [str(_POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=_POWERSHELL_ENV,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.fixture(scope="session")
def fake_llama_server_executable(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a tiny argv-agnostic fake llama server for live process tests."""
    build_dir = tmp_path_factory.mktemp("owner-llama-process")
    source = build_dir / "sleeping-process.cs"
    executable = build_dir / "sleeping-process.exe"
    source.write_text(
        r"""
using System;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;

public static class Program
{
    private static int RequestedPort(string[] args)
    {
        for (int i = 0; i + 1 < args.Length; i++)
        {
            int port;
            if (args[i] == "--port" && Int32.TryParse(args[i + 1], out port))
            {
                return port;
            }
        }
        return 0;
    }

    public static void Main(string[] args)
    {
        int port = RequestedPort(args);
        if (port <= 0)
        {
            Thread.Sleep(300000);
            return;
        }

        TcpListener listener = new TcpListener(IPAddress.Loopback, port);
        try
        {
            listener.Start();
        }
        catch
        {
            Thread.Sleep(300000);
            return;
        }

        while (true)
        {
            using (TcpClient client = listener.AcceptTcpClient())
            using (NetworkStream stream = client.GetStream())
            {
                byte[] requestBuffer = new byte[8192];
                int read = stream.Read(requestBuffer, 0, requestBuffer.Length);
                string request = Encoding.ASCII.GetString(requestBuffer, 0, read);
                string body = request.StartsWith("GET /props ", StringComparison.Ordinal)
                    ? "{\"default_generation_settings\":{\"n_ctx\":65536}}"
                    : "{\"status\":\"ok\"}";
                byte[] payload = Encoding.UTF8.GetBytes(body);
                byte[] header = Encoding.ASCII.GetBytes(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" +
                    "Content-Length: " + payload.Length +
                    "\r\nConnection: close\r\n\r\n"
                );
                stream.Write(header, 0, header.Length);
                stream.Write(payload, 0, payload.Length);
            }
        }
    }
}
""".strip(),
        encoding="utf-8",
    )
    compiler_candidates = [
        _SYSTEM_ROOT / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
        _SYSTEM_ROOT / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe",
    ]
    compiler = next((path for path in compiler_candidates if path.is_file()), None)
    if compiler is None:
        pytest.skip("Windows PowerShell C# compiler is unavailable")
    result = subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:exe",
            f"/out:{executable}",
            str(source),
        ],
        cwd=build_dir,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert executable.is_file()
    return executable


def _owner_llama_server_args(
    runtime: Path,
    model_root: Path,
    *,
    state_root: Path | None = None,
    port: int = 19178,
    context_length: int = 65536,
    reasoning_effort: str = "xhigh",
    reasoning_budget: int = 2048,
) -> list[str]:
    state_root = runtime.parent if state_root is None else state_root
    return [
        "--model",
        str(model_root / "Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf"),
        "--alias",
        "qwen38-27b-aggressive",
        "--mmproj",
        str(
            model_root
            / "mmproj-Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-BF16.gguf"
        ),
        "--image-min-tokens",
        "1024",
        "--gpu-layers",
        "all",
        "--ctx-size",
        str(context_length),
        "--parallel",
        "1",
        "--cache-type-k",
        "q8_0",
        "--cache-type-v",
        "q8_0",
        "--fit",
        "off",
        "--flash-attn",
        "on",
        "--jinja",
        "--reasoning",
        "on",
        "--reasoning-effort",
        reasoning_effort,
        "--reasoning-budget",
        str(reasoning_budget),
        "--no-reasoning-preserve",
        "--reasoning-format",
        "deepseek",
        "--temp",
        "1.0",
        "--top-p",
        "0.95",
        "--top-k",
        "20",
        "--min-p",
        "0",
        "--presence-penalty",
        "0",
        "--repeat-penalty",
        "1.0",
        "--spec-type",
        "draft-mtp",
        "--spec-draft-n-max",
        "2",
        "--spec-draft-p-min",
        "0",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--cors-origins",
        "localhost",
        "--api-key-file",
        str(state_root / "server-api-key.txt"),
        "--no-ui",
        "--slots",
    ]


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


def _seed_managed_llamacpp_manifest(
    local_app_data: Path,
    tag: str,
    *,
    backend: str = "vulkan",
    verified: bool = True,
) -> Path:
    runtime = local_app_data / "hermes" / "runtimes" / "llamacpp" / tag / backend
    runtime.mkdir(parents=True)
    manifest = {"tag": tag, "backend": backend, "assets": {}}
    if verified:
        manifest["verified_version"] = f"version: {tag.removeprefix('b')}"
    (runtime / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return runtime


def test_configure_owner_uses_external_runtime_and_private_data_dirs(tmp_path: Path):
    user_home = tmp_path / "user"
    hindsight_home = user_home / ".hindsight"
    hermes_home = tmp_path / "hermes"
    model_state = tmp_path / "model-state"
    model_runtime = model_state / "llama.cpp" / "build"
    model_runtime.mkdir(parents=True)
    model_key_path = model_state / "llama.cpp" / "server-api-key.txt"
    model_key_path.write_text("owner-test-key", encoding="utf-8")
    _seed_path_with_explicit_system_access(model_key_path)
    with pytest.raises(PermissionError):
        _validate_windows_file_owner_only(model_key_path)
    hermes_home.mkdir(parents=True)
    hermes_env_path = hermes_home / ".env"
    hermes_env_path.write_text("STALE_VALUE=preserved\n", encoding="utf-8")
    _seed_path_with_explicit_system_access(hermes_env_path)
    with pytest.raises(PermissionError, match="not current-user-only"):
        _validate_windows_file_owner_only(hermes_env_path)
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
fallback_providers:
  - provider: opencode
    model: opencode-free
local_runtime:
  enabled: true
  backend: cpu
  future_runtime_key: keep-runtime-key
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
    _seed_path_with_explicit_system_access(existing_profile_log.parent)
    _seed_path_with_explicit_system_access(user_home / ".pg0" / "instances")

    result = _run_script(
        "configure-owner-local.ps1",
        "-HermesHome",
        str(hermes_home),
        "-HermesPython",
        os.fspath(Path(os.sys.executable)),
        "-StateRoot",
        str(model_state / "llama.cpp"),
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
    assert hermes_config["fallback_providers"] == []
    assert hermes_config["local_runtime"] == {
        "enabled": False,
        "backend": "vulkan",
        "models_max": 1,
        "port": 0,
        "detect_ports": [8081],
        "future_runtime_key": "keep-runtime-key",
    }
    assert hermes_config["agent"]["future_agent_key"] == "keep-agent-key"
    assert hermes_config["display"]["future_display_key"] == "keep-display-key"
    assert hermes_config["future_root"] == {"nested": "keep-root-key"}
    assert hermes_config["agent"]["max_turns"] == 32
    assert hermes_config["agent"]["run_budget_seconds"] == 600
    assert hermes_config["agent"]["gateway_timeout"] == 600
    assert hermes_config["agent"]["turn_liveness"]["timeout_s"] == 600
    assert hermes_config["agent"]["reasoning_effort"] == "xhigh"
    assert hermes_config["model"]["max_tokens"] == 4096
    assert hermes_config["model"]["reasoning_echo"] is False
    assert hermes_config["compression"]["threshold"] == 0.75
    assert hermes_config["compression"]["threshold_tokens"] == 48000
    assert hermes_config["compression"]["max_attempts"] == 4
    assert hermes_config["compression"]["proactive_prune_tokens"] == 24000
    assert hermes_config["compression"]["proactive_prune_min_result_chars"] == 2000
    assert hermes_config["compression"]["proactive_prune_min_reclaim_tokens"] == 2048
    guardrails = hermes_config["tool_loop_guardrails"]
    assert guardrails["hard_stop_enabled"] is True
    assert guardrails["hard_stop_after"]["exact_failure"] == 3
    assert guardrails["loop_caps"] == {
        "max_web_searches": 16,
        "max_subagents": 8,
    }
    assert hermes_config["session_reset"]["mode"] == "none"
    assert hermes_config["memory"]["nudge_interval"] == 10
    assert (
        hermes_config["providers"]["local-qwen38"]["extra_body"]["reasoning_effort"]
        == "xhigh"
    )
    assert (
        hermes_config["providers"]["local-qwen38"]["extra_body"][
            "chat_template_kwargs"
        ]["preserve_thinking"]
        is False
    )
    assert hermes_config["auxiliary"]["compression"]["reasoning_effort"] == "low"
    assert (
        hermes_config["auxiliary"]["compression"]["extra_body"]["reasoning_effort"]
        == "low"
    )
    assert (
        hermes_config["providers"]["local-qwen38"]["extra_body"][
            "chat_template_kwargs"
        ]["reasoning_effort"]
        == "xhigh"
    )
    assert (
        hermes_config["auxiliary"]["background_review"]["reasoning_effort"] == "xhigh"
    )
    assert hermes_config["delegation"]["reasoning_effort"] == "xhigh"
    assert hermes_config["approvals"]["deny"] == []
    assert "# keep this owner comment" in hermes_config_text

    config = json.loads((hermes_home / "hindsight" / "config.json").read_text())
    assert config["mode"] == "local_external"
    assert config["api_url"] == "http://127.0.0.1:19177"
    assert config["profile"] == "hermes"

    hermes_env = hermes_env_path.read_text(encoding="utf-8")
    assert "STALE_VALUE=preserved" in hermes_env
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
        model_key_path,
        hermes_env_path,
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


def test_configure_persists_custom_state_root_in_startup_launchers(tmp_path: Path):
    local_app_data = tmp_path / "local app data"
    installed_scripts = (
        local_app_data / "hermes" / "hermes-agent" / "scripts"
    )
    installed_scripts.mkdir(parents=True)
    for name in ("start-owner-local-ai.ps1", "start-owner-gateway.ps1"):
        (installed_scripts / name).write_text("# launcher fixture\n", encoding="utf-8")

    state_root = tmp_path / "stable owner state"
    state_root.mkdir()
    (state_root / "server-api-key.txt").write_text(
        "owner-test-key", encoding="utf-8"
    )
    hermes_home = tmp_path / "hermes"
    hindsight_home = tmp_path / "user" / ".hindsight"
    startup_directory = tmp_path / "startup"
    env = {**_POWERSHELL_ENV, "LOCALAPPDATA": str(local_app_data)}

    # Shadow the Task Scheduler cmdlets so this test cannot inspect or mutate
    # the signed-in user's real tasks.
    command = (
        "function Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName); "
        "$null }; "
        "function Unregister-ScheduledTask { "
        "[CmdletBinding(SupportsShouldProcess=$true)] param([string]$TaskName); "
        "throw 'unexpected task mutation' }; "
        f"& {_powershell_quote(_SCRIPTS / 'configure-owner-local.ps1')} "
        f"-HermesHome {_powershell_quote(hermes_home)} "
        f"-HermesPython {_powershell_quote(os.sys.executable)} "
        f"-StateRoot {_powershell_quote(state_root)} "
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
        env=env,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    expected_argument = f'-StateRoot ""{state_root}""'
    for name in ("Hermes_Local_AI.vbs", "Hermes_Gateway.vbs"):
        launcher = (startup_directory / name).read_text(encoding="ascii")
        assert expected_argument in launcher


@pytest.mark.parametrize(
    "script_name",
    ["configure-owner-local.ps1", "start-owner-hindsight.ps1"],
)
def test_owner_entrypoints_refuse_reparse_children_without_touching_target(
    tmp_path: Path, script_name: str
):
    user_home = tmp_path / "user"
    hindsight_home = user_home / ".hindsight"
    profile_directory = hindsight_home / "profiles"
    profile_directory.mkdir(parents=True)
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    outside_file = outside_directory / "outside.txt"
    outside_file.write_text("outside survives", encoding="utf-8")
    _seed_path_with_explicit_system_access(outside_file)
    outside_acl = _dacl_sddl(outside_file)

    junction = profile_directory / "linked-outside"
    _create_directory_junction(junction, outside_directory)
    try:
        if script_name == "configure-owner-local.ps1":
            model_state = tmp_path / "model-state"
            model_runtime = model_state / "llama.cpp" / "build"
            model_runtime.mkdir(parents=True)
            (model_state / "llama.cpp" / "server-api-key.txt").write_text(
                "owner-test-key", encoding="utf-8"
            )
            result = _run_script(
                script_name,
                "-HermesHome",
                str(tmp_path / "hermes"),
                "-HermesPython",
                os.fspath(Path(os.sys.executable)),
                "-StateRoot",
                str(model_state / "llama.cpp"),
                "-HindsightRuntimeRoot",
                str(tmp_path / "hindsight-runtime"),
                "-HindsightHome",
                str(hindsight_home),
                "-SkipHindsightInstall",
                "-SkipCuaTelemetry",
                "-SkipStartupTask",
            )
        else:
            result = _run_script(
                script_name,
                "-RuntimeRoot",
                str(tmp_path / "hindsight-runtime"),
                "-HindsightHome",
                str(hindsight_home),
            )
    finally:
        # Remove only the junction itself so pytest never has to decide whether
        # recursive cleanup should traverse the out-of-scope target.
        junction.rmdir()

    assert result.returncode != 0
    assert "reparse point" in (result.stderr + result.stdout)
    assert outside_file.read_text(encoding="utf-8") == "outside survives"
    assert _dacl_sddl(outside_file) == outside_acl


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


def test_owner_config_merge_stamps_only_a_fresh_empty_config(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    overlay_path = tmp_path / "overlay.yaml"
    config_path.write_text("# fresh owner install\n", encoding="utf-8")
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

    assert result.returncode == 0, result.stderr or result.stdout
    merged = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert merged["_config_version"] == DEFAULT_CONFIG["_config_version"]
    assert merged["agent"]["reasoning_effort"] == "xhigh"


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
        f"-StateRoot {_powershell_quote(model_state / 'llama.cpp')} "
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
        _seed_path_with_explicit_system_access(profile.parent)
        _seed_path_with_explicit_system_access(profile)
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
    _assert_private_inheritable_directory(profile.parent)


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
            "-StateRoot",
            str(model_state / "llama.cpp"),
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


def test_model_verifier_reads_key_from_explicit_stable_state_root(tmp_path: Path):
    state_root = tmp_path / "stable owner state"
    state_root.mkdir()

    result = _run_script(
        "test-owner-local-ai.ps1",
        "-StateRoot",
        str(state_root),
    )

    assert result.returncode != 0
    combined_output = result.stderr + result.stdout
    assert "Local API key file is missing:" in combined_output
    assert r"stable owner state\server-api-key.txt" in combined_output


def test_model_start_defaults_to_newest_verified_managed_vulkan_runtime(
    tmp_path: Path,
):
    local_app_data = tmp_path / "local app data"
    _seed_managed_llamacpp_manifest(local_app_data, "b9999")
    expected_runtime = _seed_managed_llamacpp_manifest(local_app_data, "b10000")
    _seed_managed_llamacpp_manifest(local_app_data, "b99999", verified=False)
    _seed_managed_llamacpp_manifest(local_app_data, "b100000", backend="cpu")
    env = {**_POWERSHELL_ENV, "LOCALAPPDATA": str(local_app_data)}

    result = _run_script(
        "start-owner-local-ai.ps1",
        "-ModelRoot",
        str(tmp_path / "unused-models"),
        env=env,
    )

    combined_output = " ".join((result.stderr + result.stdout).split())
    assert result.returncode != 0
    assert "Required local-AI file is missing" in combined_output
    assert str(expected_runtime / "llama-server.exe") in combined_output


def test_model_start_explicit_runtime_overrides_managed_default(tmp_path: Path):
    local_app_data = tmp_path / "local app data"
    managed_runtime = _seed_managed_llamacpp_manifest(local_app_data, "b99999")
    explicit_runtime = tmp_path / "explicit runtime" / "build"
    env = {**_POWERSHELL_ENV, "LOCALAPPDATA": str(local_app_data)}

    result = _run_script(
        "start-owner-local-ai.ps1",
        "-RuntimeRoot",
        str(explicit_runtime),
        "-StateRoot",
        str(tmp_path / "explicit state"),
        "-ModelRoot",
        str(tmp_path / "unused-models"),
        env=env,
    )

    combined_output = " ".join((result.stderr + result.stdout).split())
    assert result.returncode != 0
    assert str(explicit_runtime / "llama-server.exe") in combined_output
    assert str(managed_runtime / "llama-server.exe") not in combined_output


def test_model_start_preserves_live_older_managed_runtime_ownership(
    tmp_path: Path,
    fake_llama_server_executable: Path,
):
    local_app_data = tmp_path / "local app data"
    running_runtime = _seed_managed_llamacpp_manifest(local_app_data, "b10000")
    selected_runtime = _seed_managed_llamacpp_manifest(local_app_data, "b10001")
    running_server = running_runtime / "llama-server.exe"
    selected_server = selected_runtime / "llama-server.exe"
    shutil.copy2(fake_llama_server_executable, running_server)
    shutil.copy2(fake_llama_server_executable, selected_server)

    model_root = tmp_path / "models"
    model_root.mkdir()
    (
        model_root / "Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf"
    ).write_bytes(b"model-fixture")
    (
        model_root
        / "mmproj-Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-BF16.gguf"
    ).write_bytes(b"projector-fixture")
    state_root = tmp_path / "stable state"
    state_root.mkdir()
    (state_root / "server-api-key.txt").write_text(
        "owner-model-test-key", encoding="utf-8"
    )
    pid_path = state_root / "server.pid"
    env = {**_POWERSHELL_ENV, "LOCALAPPDATA": str(local_app_data)}

    process = subprocess.Popen(
        [str(running_server)],
        cwd=running_runtime,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert process.poll() is None
        pid_path.write_text(str(process.pid), encoding="ascii")

        result = _run_script(
            "start-owner-local-ai.ps1",
            "-StateRoot",
            str(state_root),
            "-ModelRoot",
            str(model_root),
            env=env,
        )

        combined_output = " ".join((result.stderr + result.stdout).split())
        assert result.returncode != 0
        assert "launched with different settings" in combined_output
        assert str(selected_runtime) in combined_output
        assert process.poll() is None
        assert pid_path.read_text(encoding="ascii") == str(process.pid)
        assert not (state_root / "server.out.log").exists()
        assert not (state_root / "server.err.log").exists()
    finally:
        process.kill()
        process.wait(timeout=5)


def test_model_stop_accepts_live_verified_managed_runtime_after_newer_install(
    tmp_path: Path,
    fake_llama_server_executable: Path,
):
    local_app_data = tmp_path / "local app data"
    running_runtime = _seed_managed_llamacpp_manifest(local_app_data, "b10000")
    _seed_managed_llamacpp_manifest(local_app_data, "b10001")
    server = running_runtime / "llama-server.exe"
    shutil.copy2(fake_llama_server_executable, server)
    state_root = tmp_path / "stable state"
    state_root.mkdir()
    pid_path = state_root / "server.pid"
    env = {**_POWERSHELL_ENV, "LOCALAPPDATA": str(local_app_data)}

    process = subprocess.Popen(
        [str(server)],
        cwd=running_runtime,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert process.poll() is None
        pid_path.write_text(str(process.pid), encoding="ascii")
        result = _run_script(
            "stop-owner-local-ai.ps1",
            "-StateRoot",
            str(state_root),
            env=env,
        )

        assert result.returncode == 0, result.stderr or result.stdout
        process.wait(timeout=5)
        assert not pid_path.exists()
        assert f"Local AI stopped (PID {process.pid})." in result.stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_model_stop_refuses_unmanaged_runtime_without_explicit_override(
    tmp_path: Path,
    fake_llama_server_executable: Path,
):
    local_app_data = tmp_path / "local app data"
    local_app_data.mkdir()
    runtime = tmp_path / "unmanaged runtime"
    runtime.mkdir()
    server = runtime / "llama-server.exe"
    shutil.copy2(fake_llama_server_executable, server)
    state_root = tmp_path / "stable state"
    state_root.mkdir()
    pid_path = state_root / "server.pid"
    env = {**_POWERSHELL_ENV, "LOCALAPPDATA": str(local_app_data)}

    process = subprocess.Popen(
        [str(server)],
        cwd=runtime,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert process.poll() is None
        pid_path.write_text(str(process.pid), encoding="ascii")
        result = _run_script(
            "stop-owner-local-ai.ps1",
            "-StateRoot",
            str(state_root),
            env=env,
        )

        assert result.returncode != 0
        assert "not a verified Hermes-managed Vulkan llama-server" in (
            result.stderr + result.stdout
        )
        assert process.poll() is None
        assert pid_path.exists()
    finally:
        process.kill()
        process.wait(timeout=5)


def test_model_stop_explicit_runtime_remains_authoritative(
    tmp_path: Path,
    fake_llama_server_executable: Path,
):
    runtime = tmp_path / "explicit runtime"
    runtime.mkdir()
    server = runtime / "llama-server.exe"
    shutil.copy2(fake_llama_server_executable, server)
    state_root = tmp_path / "stable state"
    state_root.mkdir()
    pid_path = state_root / "server.pid"

    process = subprocess.Popen(
        [str(server)],
        cwd=runtime,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert process.poll() is None
        pid_path.write_text(str(process.pid), encoding="ascii")
        result = _run_script(
            "stop-owner-local-ai.ps1",
            "-StateRoot",
            str(state_root),
            "-RuntimeRoot",
            str(runtime),
        )

        assert result.returncode == 0, result.stderr or result.stdout
        process.wait(timeout=5)
        assert not pid_path.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_model_start_keeps_default_state_under_existing_g_drive_location(
    tmp_path: Path,
):
    local_app_data = tmp_path / "local app data"
    runtime = _seed_managed_llamacpp_manifest(local_app_data, "b10000")
    (runtime / "llama-server.exe").write_bytes(b"not-started-by-this-test")
    model_root = tmp_path / "models"
    model_root.mkdir()
    (
        model_root / "Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf"
    ).write_bytes(b"model-fixture")
    (
        model_root / "mmproj-Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-BF16.gguf"
    ).write_bytes(b"projector-fixture")
    env = {**_POWERSHELL_ENV, "LOCALAPPDATA": str(local_app_data)}

    # Intercept the first state-file probe before the launcher can read or
    # write the operator's real G: state. All fixture/path checks still run
    # through the native Test-Path cmdlet.
    command = (
        "function Test-Path { [CmdletBinding()] param("
        "[Parameter(Mandatory=$true)][string[]]$LiteralPath,"
        "[Microsoft.PowerShell.Commands.TestPathType]$PathType); "
        "$candidate=[string]$LiteralPath[0]; "
        "if ([System.IO.Path]::GetFileName($candidate) -eq "
        "'server-api-key.txt') { throw ('OWNER_STATE_PROBE|' + $candidate) }; "
        "Microsoft.PowerShell.Management\\Test-Path "
        "-LiteralPath $LiteralPath -PathType $PathType }; "
        f"& {_powershell_quote(_SCRIPTS / 'start-owner-local-ai.ps1')} "
        f"-ModelRoot {_powershell_quote(model_root)}"
    )
    result = subprocess.run(
        [str(_POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )

    combined_output = " ".join((result.stderr + result.stdout).split())
    assert result.returncode != 0
    assert (
        r"OWNER_STATE_PROBE|G:\LocalAI\llama.cpp\server-api-key.txt"
        in combined_output
    )


def test_model_start_replaces_stale_explicit_api_key_acl(tmp_path: Path):
    model_state = tmp_path / "model-state"
    runtime = model_state / "llama.cpp" / "build"
    model_root = tmp_path / "models"
    runtime.mkdir(parents=True)
    model_root.mkdir(parents=True)

    # The invalid executable intentionally stops the script immediately after
    # its pre-launch key handling; no model process is started by this test.
    (runtime / "llama-server.exe").write_bytes(b"not-a-windows-executable")
    (model_root / "Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf").write_bytes(
        b"model-fixture"
    )
    (
        model_root / "mmproj-Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-BF16.gguf"
    ).write_bytes(b"projector-fixture")
    key_path = model_state / "llama.cpp" / "server-api-key.txt"
    key_path.write_text("owner-model-test-key", encoding="utf-8")
    _seed_path_with_explicit_system_access(key_path)

    result = _run_script(
        "start-owner-local-ai.ps1",
        "-RuntimeRoot",
        str(runtime),
        "-StateRoot",
        str(model_state / "llama.cpp"),
        "-ModelRoot",
        str(model_root),
        "-Port",
        "19178",
    )

    assert result.returncode != 0
    _validate_windows_file_owner_only(key_path)
    assert key_path.read_text(encoding="utf-8") == "owner-model-test-key"


@pytest.mark.parametrize(
    "changed_option",
    [
        "--model",
        "--alias",
        "--mmproj",
        "--image-min-tokens",
        "--gpu-layers",
        "--ctx-size",
        "--parallel",
        "--cache-type-k",
        "--cache-type-v",
        "--fit",
        "--flash-attn",
        "--jinja",
        "--reasoning",
        "--reasoning-effort",
        "--reasoning-budget",
        "--no-reasoning-preserve",
        "--reasoning-format",
        "--temp",
        "--top-p",
        "--top-k",
        "--min-p",
        "--presence-penalty",
        "--repeat-penalty",
        "--spec-type",
        "--spec-draft-n-max",
        "--spec-draft-p-min",
        "--host",
        "--port",
        "--cors-origins",
        "--api-key-file",
        "--no-ui",
        "--slots",
    ],
)
def test_model_start_refuses_same_binary_with_changed_owner_argument(
    tmp_path: Path,
    fake_llama_server_executable: Path,
    changed_option: str,
):
    runtime = tmp_path / "model runtime" / "bin"
    runtime.mkdir(parents=True)
    server = runtime / "llama-server.exe"
    shutil.copy2(fake_llama_server_executable, server)

    model_root = tmp_path / "owner models"
    model_root.mkdir()
    (
        model_root / "Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf"
    ).write_bytes(b"model-fixture")
    (
        model_root / "mmproj-Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-BF16.gguf"
    ).write_bytes(b"projector-fixture")

    state_root = runtime.parent
    key_path = state_root / "server-api-key.txt"
    key_path.write_text("owner-model-test-key", encoding="utf-8")
    expected_args = _owner_llama_server_args(runtime, model_root)
    live_args = expected_args.copy()
    valueless_options = {
        "--jinja",
        "--no-reasoning-preserve",
        "--no-ui",
        "--slots",
    }
    changed_index = live_args.index(changed_option)
    if changed_option in valueless_options:
        live_args.pop(changed_index)
    else:
        live_args[changed_index + 1] += "-stale"

    process = subprocess.Popen(
        [str(server), *live_args],
        cwd=runtime,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert process.poll() is None
        (state_root / "server.pid").write_text(str(process.pid), encoding="ascii")
        result = _run_script(
            "start-owner-local-ai.ps1",
            "-RuntimeRoot",
            str(runtime),
            "-StateRoot",
            str(state_root),
            "-ModelRoot",
            str(model_root),
            "-Port",
            "19178",
        )

        combined_output = result.stderr + result.stdout
        normalized_output = " ".join(combined_output.split())
        assert result.returncode != 0
        assert "launched with different settings" in normalized_output
        assert "requested settings were NOT applied" in normalized_output
        assert f"Stop-Process -Id {process.pid}" in normalized_output
        assert "Then restart it exactly with:" in normalized_output
        assert "-StateRoot" in normalized_output
        assert process.poll() is None
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_model_start_reuses_same_binary_only_when_full_contract_matches(
    tmp_path: Path,
    fake_llama_server_executable: Path,
):
    runtime = tmp_path / "model runtime" / "bin"
    runtime.mkdir(parents=True)
    server = runtime / "llama-server.exe"
    shutil.copy2(fake_llama_server_executable, server)

    model_root = tmp_path / "owner models"
    model_root.mkdir()
    (
        model_root / "Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-Q4_K_P.gguf"
    ).write_bytes(b"model-fixture")
    (
        model_root / "mmproj-Qwen3.8-27B-Uncensored-HauhauCS-Aggressive-BF16.gguf"
    ).write_bytes(b"projector-fixture")

    state_root = runtime.parent
    (state_root / "server-api-key.txt").write_text(
        "owner-model-test-key", encoding="utf-8"
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    expected_args = _owner_llama_server_args(runtime, model_root, port=port)
    # Windows paths are case-insensitive; a semantic comparison must not reject
    # a process merely because CIM/another launcher preserved different casing.
    live_args = expected_args.copy()
    for path_option in ("--model", "--mmproj", "--api-key-file"):
        value_index = live_args.index(path_option) + 1
        live_args[value_index] = live_args[value_index].swapcase()
    process = subprocess.Popen(
        [str(server).swapcase(), *live_args],
        cwd=runtime,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert process.poll() is None
        (state_root / "server.pid").write_text(str(process.pid), encoding="ascii")
        result = _run_script(
            "start-owner-local-ai.ps1",
            "-RuntimeRoot",
            str(runtime),
            "-StateRoot",
            str(state_root),
            "-ModelRoot",
            str(model_root),
            "-Port",
            str(port),
            "-HindsightRuntimeRoot",
            str(tmp_path / "missing-hindsight-runtime"),
            "-HindsightHome",
            str(tmp_path / "user" / ".hindsight"),
            "-HindsightPort",
            "19179",
        )

        combined_output = " ".join((result.stderr + result.stdout).split())
        assert result.returncode != 0
        assert "launched with different settings" not in combined_output
        assert "Isolated Hindsight runtime was not found" in combined_output
        assert process.poll() is None
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_owner_powershell_entrypoints_parse_under_windows_powershell_51():
    paths = [
        _SCRIPTS / "windows-owner-acl.ps1",
        _SCRIPTS / "configure-owner-local.ps1",
        _SCRIPTS / "start-owner-local-ai.ps1",
        _SCRIPTS / "start-owner-hindsight.ps1",
        _SCRIPTS / "start-owner-gateway.ps1",
        _SCRIPTS / "stop-owner-local-ai.ps1",
        _SCRIPTS / "test-owner-local-ai.ps1",
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
