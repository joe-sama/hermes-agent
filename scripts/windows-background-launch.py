"""Windowless owner-startup trampoline; run with pythonw.exe on Windows.

GUI apps such as Electron can AttachConsole to their parent even when launched
with SW_HIDE. A windowless parent plus independent stdio prevents that console
from surviving the startup script (or closing the app when the terminal closes).
This helper exits after spawning; it is not a service or a supervisor.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import traceback
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("ab", buffering=0) as log:
        try:
            if sys.platform != "win32":
                raise RuntimeError("This launcher requires Windows and pythonw.exe")
            if not command or not Path(command[0]).is_absolute() or not Path(command[0]).is_file():
                raise ValueError("An existing absolute executable path is required")
            subprocess.Popen(
                command,
                cwd=args.cwd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                close_fds=True,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except Exception:
            log.write(traceback.format_exc().encode("utf-8", errors="replace"))
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
