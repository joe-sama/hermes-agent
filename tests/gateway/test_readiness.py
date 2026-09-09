from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from gateway.readiness import collect_runtime_readiness


def test_collect_runtime_readiness_reports_healthy_local_runtime(tmp_path, monkeypatch):
    # Disk capacity belongs to this scenario, not the machine running pytest.
    # A developer's nearly-full system drive must not invalidate a healthy
    # config/database integration test. Keep the real filesystem/SQLite path.
    monkeypatch.setattr(
        "gateway.readiness.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=1000, used=200, free=800),
    )
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  model: test/model\n",
        encoding="utf-8",
    )
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {"telegram": {"state": "connected"}},
            "updated_at": "2026-07-09T00:00:00Z",
        },
        active_api_runs=2,
    )

    assert result["status"] == "ok"
    assert result["checks"]["state_db"]["status"] == "ok"
    assert result["checks"]["session_store"]["status"] == "ok"
    assert result["checks"]["config"]["status"] == "ok"
    assert result["checks"]["model"]["status"] == "ok"
    assert result["checks"]["gateway"]["status"] == "ok"
    assert result["checks"]["background_queues"]["active_api_runs"] == 2
    assert result["checks"]["disk"]["status"] == "ok"


def test_disk_pressure_is_reported_without_mutating_state(tmp_path, monkeypatch):
    from gateway.readiness import _probe_disk

    existing_entries = set(tmp_path.iterdir())
    monkeypatch.setattr(
        "gateway.readiness.shutil.disk_usage",
        lambda _path: SimpleNamespace(total=1000, used=950, free=50),
    )
    assert _probe_disk(tmp_path) == {"status": "degraded", "used_percent": 95.0, "free_bytes": 50}
    assert set(tmp_path.iterdir()) == existing_entries


def test_collect_runtime_readiness_degrades_on_invalid_config_and_stopped_gateway(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: [unterminated", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="",
        runtime_status={"gateway_state": "stopped", "platforms": {}},
    )

    assert result["status"] == "degraded"
    assert result["checks"]["config"]["status"] == "degraded"
    assert result["checks"]["model"]["status"] == "degraded"
    assert result["checks"]["gateway"]["status"] == "degraded"
    # Readiness is diagnostic data, not an exception or a destructive repair.
    assert (home / "config.yaml").read_text(encoding="utf-8") == "model: [unterminated"


def test_readiness_uses_running_session_store_state_over_independent_probe(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    monkeypatch.setenv("HERMES_HOME", str(home))

    unavailable = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {},
            "session_store": {"status": "unavailable"},
        },
    )

    assert unavailable["checks"]["state_db"]["status"] == "ok"
    assert unavailable["checks"]["session_store"] == {"status": "unavailable"}
    assert unavailable["status"] == "degraded"

    recovered = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {},
            "session_store": {"status": "ok"},
        },
    )
    assert recovered["checks"]["session_store"] == {"status": "ok"}
