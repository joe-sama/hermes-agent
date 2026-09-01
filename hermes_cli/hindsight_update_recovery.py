"""Fresh-process recovery for Hindsight daemons paused by ``hermes update``.

The updater that replaces the checkout keeps executing old imported code.
This helper is deliberately launched through the post-update venv so both
Hermes and ``hindsight_embed`` are imported from the completed installation.
"""

from __future__ import annotations

import argparse
import json
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


def recover(profile: str, port: int) -> int:
    """Start one profile and verify the recorded loopback endpoint."""
    try:
        from hindsight_embed import DaemonEmbedManager
        from hindsight_embed.profile_manager import ProfileManager

        resolved = ProfileManager().resolve_profile_paths(profile)
        if int(resolved.port) != port:
            print("Hindsight profile port changed; refusing update recovery", file=sys.stderr)
            return 2
        if not DaemonEmbedManager().ensure_running({}, profile):
            print("Hindsight daemon did not start", file=sys.stderr)
            return 1
    except Exception as exc:
        print(f"Hindsight update recovery failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _health_ok(port):
            return 0
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
