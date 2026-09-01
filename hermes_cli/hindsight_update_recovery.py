"""Fresh-process recovery for Hindsight daemons paused by ``hermes update``.

The updater that replaces the checkout keeps executing old imported code.
This helper is deliberately launched through the post-update venv so both
Hermes and ``hindsight_embed`` are imported from the completed installation.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
import urllib.request


def _health_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(  # noqa: S310 - fixed loopback URL
            f"http://127.0.0.1:{port}/health", timeout=2
        ) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
        return (
            isinstance(payload, dict)
            and payload.get("status") == "healthy"
            and payload.get("database") == "connected"
        )
    except Exception:
        return False


def _port_is_free(port: int) -> bool:
    """Return True only when the recorded IPv4 loopback port can be bound."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _no_reclaim_manager(base_class, daemon_module, port: int):
    """Construct a feature-gated manager that cannot reclaim a listener.

    Hindsight's normal startup deliberately kills an unhealthy process that
    occupies the configured port. That policy is inappropriate in an updater
    recovery callback: another application may have claimed the port while a
    long update was running. This private fallback therefore requires every
    method it overrides and fails closed when Hindsight changes underneath it.
    """
    required = (
        "_clear_port",
        "_kill_process",
        "_start_daemon_locked",
        "_find_pid_on_port",
        "ensure_running",
    )
    if any(not callable(getattr(base_class, name, None)) for name in required):
        raise RuntimeError("unsupported hindsight-embed recovery API")
    if not callable(getattr(getattr(daemon_module, "subprocess", None), "Popen", None)):
        raise RuntimeError("unsupported hindsight-embed spawn API")

    class NoReclaimDaemonManager(base_class):
        def __init__(self):
            super().__init__()
            self.update_recovery_launched_pids: dict[int, float] = {}

        def _clear_port(self, candidate_port: int) -> bool:
            return int(candidate_port) == port and _port_is_free(port)

        @staticmethod
        def _kill_process(_pid: int) -> bool:
            raise RuntimeError("update recovery refuses process termination")

        def _start_daemon_locked(self, *args, **kwargs) -> bool:
            original_popen = daemon_module.subprocess.Popen

            def tracked_popen(*popen_args, **popen_kwargs):
                process = original_popen(*popen_args, **popen_kwargs)
                # PID alone is not an identity: Windows can reuse it while a
                # slow daemon launch is being verified. Capture creation time
                # immediately and fail closed later if it cannot be proven.
                try:
                    import psutil

                    created = float(psutil.Process(int(process.pid)).create_time())
                except Exception:
                    created = 0.0
                self.update_recovery_launched_pids[int(process.pid)] = created
                return process

            daemon_module.subprocess.Popen = tracked_popen
            try:
                return bool(super()._start_daemon_locked(*args, **kwargs))
            finally:
                daemon_module.subprocess.Popen = original_popen

    return NoReclaimDaemonManager()


def _listener_belongs_to_launch(manager, port: int) -> bool:
    """Prove the healthy listener is the process this recovery just launched."""
    launched = getattr(manager, "update_recovery_launched_pids", {})
    if not isinstance(launched, dict):
        return False
    if not launched:
        return False
    try:
        import psutil

        listener_pid = manager._find_pid_on_port(port)
        if listener_pid is None:
            return False
        listener = psutil.Process(int(listener_pid))
        lineage = [listener, *listener.parents()]
        for process in lineage:
            expected_created = float(launched.get(int(process.pid)) or 0.0)
            if expected_created <= 0:
                continue
            if abs(float(process.create_time()) - expected_created) <= 0.01:
                return True
        return False
    except Exception:
        return False


def recover(profile: str, port: int) -> int:
    """Start one profile without reclaiming ports and verify its identity."""
    try:
        from hindsight_embed import DaemonEmbedManager
        from hindsight_embed import daemon_embed_manager as daemon_module
        from hindsight_embed.profile_manager import ProfileManager

        resolved = ProfileManager().resolve_profile_paths(profile)
        if int(resolved.port) != port:
            print(
                "Hindsight profile port changed; refusing update recovery",
                file=sys.stderr,
            )
            return 2
        if not _port_is_free(port):
            print(
                "Hindsight recovery port is occupied; refusing to reclaim it",
                file=sys.stderr,
            )
            return 2
        manager = _no_reclaim_manager(DaemonEmbedManager, daemon_module, port)
        if not manager.ensure_running({}, profile):
            print("Hindsight daemon did not start", file=sys.stderr)
            return 1
        if not _listener_belongs_to_launch(manager, port):
            print("Hindsight listener identity could not be verified", file=sys.stderr)
            return 2
    except Exception as exc:
        print(
            f"Hindsight update recovery failed: {type(exc).__name__}", file=sys.stderr
        )
        return 1

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _health_ok(port):
            # A healthy loopback endpoint is not enough: the launched daemon
            # can die and an unrelated process can claim the port between the
            # initial lineage proof and this poll. Revalidate immediately
            # before blessing the recovery.
            if _listener_belongs_to_launch(manager, port):
                return 0
            print(
                "Hindsight listener identity changed during health verification",
                file=sys.stderr,
            )
            return 2
        time.sleep(0.25)
    print("Hindsight daemon failed its post-update health check", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    return recover(args.profile, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
