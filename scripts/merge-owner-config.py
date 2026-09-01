#!/usr/bin/env python3
"""Merge owner-controlled Hermes settings without replacing user config."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_cli.config import require_readable_config_before_write  # noqa: E402
from utils import atomic_roundtrip_yaml_save, fast_safe_load  # noqa: E402


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Replace overlay leaves while preserving unrelated existing paths."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = deepcopy(value)
    return base


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: merge-owner-config.py CONFIG_PATH OVERLAY_PATH")

    config_path = Path(sys.argv[1])
    overlay_path = Path(sys.argv[2])
    with overlay_path.open(encoding="utf-8") as stream:
        overlay = fast_safe_load(stream)
    if not isinstance(overlay, dict):
        raise SystemExit("owner configuration overlay must be a YAML mapping")

    existing = require_readable_config_before_write(config_path)
    atomic_roundtrip_yaml_save(config_path, _deep_merge(existing, overlay))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
