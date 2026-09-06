#!/usr/bin/env python3
"""Merge owner-controlled Hermes settings without replacing user config."""

from __future__ import annotations

import argparse
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_cli.config import DEFAULT_CONFIG, require_readable_config_before_write  # noqa: E402
from utils import atomic_roundtrip_yaml_save, fast_safe_load  # noqa: E402


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Replace overlay leaves while preserving unrelated existing paths."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = deepcopy(value)
    return base


def _remove_path(config: dict[str, Any], dotted_path: str) -> None:
    """Remove one exact dotted config leaf without touching siblings."""
    parts = [part for part in dotted_path.split(".") if part]
    if not parts or len(parts) != len(dotted_path.split(".")):
        raise SystemExit(f"invalid config removal path: {dotted_path!r}")
    parent: Any = config
    for part in parts[:-1]:
        if not isinstance(parent, dict) or part not in parent:
            return
        parent = parent[part]
    if isinstance(parent, dict):
        parent.pop(parts[-1], None)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge owner-controlled Hermes settings")
    parser.add_argument("config_path")
    parser.add_argument("overlay_path")
    parser.add_argument(
        "--remove", action="append", default=[], metavar="DOTTED_PATH",
        help="remove one obsolete exact config path after merging",
    )
    args = parser.parse_args()

    config_path = Path(args.config_path)
    overlay_path = Path(args.overlay_path)
    with overlay_path.open(encoding="utf-8") as stream:
        overlay = fast_safe_load(stream)
    if not isinstance(overlay, dict):
        raise SystemExit("owner configuration overlay must be a YAML mapping")

    existing = require_readable_config_before_write(config_path)
    existing_was_empty = not existing
    merged = _deep_merge(existing, overlay)
    for dotted_path in args.remove:
        _remove_path(merged, dotted_path)
    # A genuinely fresh owner install has no migrations to apply, but it must
    # still carry the current schema stamp.  Never overwrite an explicit (even
    # future) version, and never stamp a non-empty hand-written legacy config:
    # those must go through Hermes' real migration ladder.
    if existing_was_empty and "_config_version" not in merged:
        merged["_config_version"] = int(DEFAULT_CONFIG["_config_version"])
    atomic_roundtrip_yaml_save(config_path, merged)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
