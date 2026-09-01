"""Windows hand-off child must hard-exit once the update is durably done (#93581).

The re-exec'd venv child (spawned by
``_reexec_dependency_sync_off_windows_shim`` with ``HERMES_UPDATE_REEXEC=1``)
completes all update work — the receipt records ``success`` / ``completed at
command boundary`` — but then hangs in interpreter shutdown on a leftover
non-daemon thread, freezing the PowerShell window for minutes. The fix: on
the hand-off path only, after the receipt is finalized, the lock released,
and stdio restored, flush and ``os._exit(code)`` instead of unwinding.

These tests pin: the hard exit fires (with the right code) only when the
re-exec marker env is set, it happens after lock release + stdio restore,
early ``SystemExit`` codes propagate to it, and real exceptions keep the
normal raise path (traceback intact, no hard exit).
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import hermes_cli.main as main_mod
from hermes_cli.main import cmd_update


class _FakeLock:
    def __init__(self, events):
        self._events = events

    def acquire(self):
        self._events.append("acquire")
        return True

    def release(self):
        self._events.append("release")


# Events from the most recent _run_cmd_update call, also filled in when
# cmd_update propagates an exception (the return value is unreachable then).
_LAST = {}


def _run_cmd_update(monkeypatch, impl, *, reexec: bool):
    """Run cmd_update with everything external mocked; return the events."""
    events = {"order": [], "exit_codes": [], "receipts": []}
    main_mod._UPDATE_EXIT_RECOVERIES.clear()
    main_mod._UPDATE_TRANSFERABLE_RECOVERIES.clear()
    monkeypatch.setattr(main_mod, "_UPDATE_RECOVERY_ACK_COMMITTED", False)
    monkeypatch.setattr(main_mod, "_UPDATE_RECOVERY_OWNERSHIP_COMMITTED", False)
    monkeypatch.setattr(main_mod, "_UPDATE_REEXEC_PENDING_MARKER_TRANSFER", None)

    def fake_impl(args, gateway_mode=False):
        events["order"].append("impl")
        impl(args, gateway_mode=gateway_mode)

    def fake_finalize_io(state):
        events["order"].append("restore-stdio")

    def fake_receipt(code, reason):
        events["order"].append("finalize-receipt")
        events["receipts"].append((code, reason))

    def fake_exit(code):
        events["order"].append("hard-exit")
        events["exit_codes"].append(code)

    def fake_adopt():
        events["order"].append("adopt-recoveries")
        return True

    real_run_recoveries = main_mod._run_update_exit_recoveries

    def tracked_run_recoveries():
        events["order"].append("recover-services")
        return real_run_recoveries()

    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda root: "git")
    monkeypatch.setattr(
        "hermes_cli.update_lock.UpdateLock", lambda: _FakeLock(events["order"])
    )
    monkeypatch.setattr(main_mod, "_cmd_update_impl", fake_impl)
    monkeypatch.setattr(main_mod, "_adopt_transferred_update_recoveries", fake_adopt)
    monkeypatch.setattr(main_mod, "_UPDATE_REEXEC_PARENT_HARD_EXIT", False)
    monkeypatch.setattr(
        main_mod, "_install_hangup_protection", lambda gateway_mode=False: None
    )
    monkeypatch.setattr(main_mod, "_finalize_update_output", fake_finalize_io)
    monkeypatch.setattr(main_mod, "_run_update_exit_recoveries", tracked_run_recoveries)
    monkeypatch.setattr(
        "hermes_cli.update_receipt.finalize_pending_update_receipt", fake_receipt
    )
    monkeypatch.setattr("os._exit", fake_exit)
    if reexec:
        monkeypatch.setenv("HERMES_UPDATE_REEXEC", "1")
    else:
        monkeypatch.delenv("HERMES_UPDATE_REEXEC", raising=False)

    args = SimpleNamespace(plan=False, check=False, gateway=False, branch=None)
    try:
        cmd_update(args)
    finally:
        _LAST.clear()
        _LAST.update(events)
    return events


def _noop_impl(args, gateway_mode=False):
    return None


def test_handoff_child_hard_exits_zero_after_success(monkeypatch):
    events = _run_cmd_update(monkeypatch, _noop_impl, reexec=True)
    assert events["exit_codes"] == [0]
    assert events["receipts"] == [(0, "completed at command boundary")]
    # Services recover before success is made durable. The hard exit is the
    # last thing, after lock release and stdio restore.
    assert events["order"] == [
        "adopt-recoveries",
        "acquire",
        "impl",
        "recover-services",
        "finalize-receipt",
        "release",
        "restore-stdio",
        "hard-exit",
    ]


def test_recovery_transfer_adoption_precedes_update_lock(monkeypatch):
    events = _run_cmd_update(monkeypatch, _noop_impl, reexec=False)
    assert events["order"].index("adopt-recoveries") < events["order"].index(
        "acquire"
    )


def test_non_handoff_run_never_hard_exits(monkeypatch):
    events = _run_cmd_update(monkeypatch, _noop_impl, reexec=False)
    assert events["exit_codes"] == []
    assert "hard-exit" not in events["order"]
    assert events["receipts"] == [(0, "completed at command boundary")]


def test_handoff_child_propagates_early_systemexit_code(monkeypatch):
    def early_refusal(args, gateway_mode=False):
        raise SystemExit(3)

    with pytest.raises(SystemExit) as excinfo:
        _run_cmd_update(monkeypatch, early_refusal, reexec=True)
    assert excinfo.value.code == 3
    # The finally-block hard exit ran (before the re-raise propagated)
    # and carried the early exit's code, not a blanket 0.
    assert _LAST["exit_codes"] == [3]


def test_handoff_child_systemexit_none_means_zero(monkeypatch):
    def bare_exit(args, gateway_mode=False):
        raise SystemExit(None)

    with pytest.raises(SystemExit):
        _run_cmd_update(monkeypatch, bare_exit, reexec=True)
    assert _LAST["exit_codes"] == [0]


def test_acknowledged_parent_hard_exits_without_finalizing_child_receipt(monkeypatch):
    def completed_transfer(args, gateway_mode=False):
        main_mod._UPDATE_REEXEC_PARENT_HARD_EXIT = True
        raise SystemExit(0)

    with pytest.raises(SystemExit) as excinfo:
        _run_cmd_update(monkeypatch, completed_transfer, reexec=False)

    assert excinfo.value.code == 0
    assert _LAST["receipts"] == []
    assert _LAST["exit_codes"] == [0]
    assert _LAST["order"] == [
        "adopt-recoveries",
        "acquire",
        "impl",
        "recover-services",
        "release",
        "restore-stdio",
        "hard-exit",
    ]


def test_interrupted_marker_transfer_readback_suppresses_parent_outcome(monkeypatch):
    def interrupted_after_replace(args, gateway_mode=False):
        main_mod._UPDATE_REEXEC_PENDING_MARKER_TRANSFER = (111, 222, None)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "hermes_cli.update_lock.marker_handoff_state",
        lambda source, successor, preserve: "successor",
    )

    with pytest.raises(KeyboardInterrupt):
        _run_cmd_update(monkeypatch, interrupted_after_replace, reexec=False)

    assert _LAST["receipts"] == []
    assert _LAST["exit_codes"] == [0]
    assert _LAST["order"] == [
        "adopt-recoveries",
        "acquire",
        "impl",
        "recover-services",
        "release",
        "restore-stdio",
        "hard-exit",
    ]


def test_unhandled_exception_keeps_raise_path_no_hard_exit(monkeypatch):
    def boom(args, gateway_mode=False):
        raise RuntimeError("update tail exploded")

    # With os._exit patched to record, the re-raised RuntimeError reaches
    # pytest with the finally block already run — and no hard exit fires.
    with pytest.raises(RuntimeError, match="update tail exploded"):
        _run_cmd_update(monkeypatch, boom, reexec=True)
    assert "hard-exit" not in _LAST["order"]
    assert _LAST["receipts"] == [(1, "RuntimeError: update tail exploded")]


def test_update_exit_recovery_runs_once_before_hard_exit(monkeypatch):
    recovered: list[str] = []

    def register_recovery(args, gateway_mode=False):
        main_mod._register_update_exit_recovery(recovered.append, "hindsight")

    events = _run_cmd_update(monkeypatch, register_recovery, reexec=True)

    assert recovered == ["hindsight"]
    assert events["order"].index("recover-services") < events["order"].index(
        "finalize-receipt"
    )
    assert events["order"].index("recover-services") < events["order"].index(
        "hard-exit"
    )
    assert main_mod._UPDATE_EXIT_RECOVERIES == []


def test_update_exit_recovery_failure_marks_receipt_failed(monkeypatch):
    def register_failed_recovery(args, gateway_mode=False):
        main_mod._register_update_exit_recovery(lambda: False)

    with pytest.raises(SystemExit) as excinfo:
        _run_cmd_update(monkeypatch, register_failed_recovery, reexec=True)

    assert excinfo.value.code == 1
    assert _LAST["exit_codes"] == [1]
    assert _LAST["receipts"] == [(1, "update exit recovery failed")]
    assert _LAST["order"].index("recover-services") < _LAST["order"].index(
        "finalize-receipt"
    )
    assert _LAST["order"].index("finalize-receipt") < _LAST["order"].index(
        "hard-exit"
    )


def test_impl_has_no_restart_branch_terminal_marker_before_durable_tail():
    """All restart failures converge before the watcher sees a terminal file."""
    from hermes_cli import update_cmd

    source = inspect.getsource(update_cmd._cmd_update_impl)
    assert '".update_exit_code"' not in source
    # ZIP-primary, normal-git, and ZIP-fallback success each publish only
    # through the atomic helper. Restart-failure branches publish at the outer
    # command boundary instead of writing the marker themselves.
    assert source.count("_write_gateway_update_exit_code(") == 3
    terminal = source.rindex("_write_gateway_update_exit_code(")
    recovery = source.rindex("_run_update_exit_recoveries()", 0, terminal)
    receipt = source.rindex("finalize_update_receipt(", 0, terminal)
    assert recovery < receipt < terminal


@pytest.mark.parametrize(
    "failure_shape",
    ["failed-or-stale-units", "restart-phase-aborted", "windows-resume-error"],
)
def test_gateway_restart_failure_shapes_publish_once_after_recovery_and_receipt(
    monkeypatch, failure_shape
):
    """The three restart failure families share one durable command boundary."""
    from hermes_cli import update_cmd, update_receipt

    events = []
    main_mod._UPDATE_EXIT_RECOVERIES.clear()
    main_mod._UPDATE_TRANSFERABLE_RECOVERIES.clear()
    monkeypatch.setattr(main_mod, "_UPDATE_RECOVERY_ACK_COMMITTED", False)
    monkeypatch.setattr(main_mod, "_UPDATE_RECOVERY_OWNERSHIP_COMMITTED", False)
    monkeypatch.setattr(main_mod, "_adopt_transferred_update_recoveries", lambda: False)
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr(
        "hermes_cli.update_contract.evaluate_update_admission", lambda _root: None
    )
    monkeypatch.setattr(
        "hermes_cli.update_lock.UpdateLock", lambda: _FakeLock(events)
    )
    monkeypatch.setattr(
        main_mod, "_install_hangup_protection", lambda gateway_mode=False: None
    )
    monkeypatch.setattr(main_mod, "_finalize_update_output", lambda _state: None)

    def fail_after_restart_accounting(args, gateway_mode=False):
        assert gateway_mode is True
        events.append(f"gateway-result:{failure_shape}")
        main_mod._register_update_exit_recovery(
            lambda: events.append("exit-recovery") or True
        )
        raise SystemExit(1)

    monkeypatch.setattr(main_mod, "_cmd_update_impl", fail_after_restart_accounting)
    monkeypatch.setattr(
        update_receipt,
        "finalize_pending_update_receipt",
        lambda *_a, **_k: events.append("receipt"),
    )
    monkeypatch.setattr(
        update_cmd,
        "_write_gateway_update_exit_code",
        lambda ok: events.append(f"terminal:{int(not ok)}"),
    )

    with pytest.raises(SystemExit) as excinfo:
        cmd_update(
            SimpleNamespace(gateway=True, plan=False, check=False, branch=None)
        )

    assert excinfo.value.code == 1
    durable = [
        event
        for event in events
        if event.startswith("gateway-result:")
        or event in {"exit-recovery", "receipt", "terminal:1"}
    ]
    assert durable == [
        f"gateway-result:{failure_shape}",
        "exit-recovery",
        "receipt",
        "terminal:1",
    ]
    assert events.count("terminal:1") == 1


@pytest.mark.parametrize("recovery_ok", [True, False])
def test_post_accept_adoption_interrupt_persists_failure_before_gateway_status(
    monkeypatch, recovery_ok
):
    from hermes_cli import update_cmd, update_receipt

    events = []
    main_mod._UPDATE_EXIT_RECOVERIES.clear()
    main_mod._UPDATE_TRANSFERABLE_RECOVERIES.clear()
    monkeypatch.setattr(main_mod, "_UPDATE_RECOVERY_ACK_COMMITTED", True)
    monkeypatch.setattr(main_mod, "_UPDATE_RECOVERY_OWNERSHIP_COMMITTED", False)

    def recover():
        events.append("recover")
        return recovery_ok

    def interrupted_adopt():
        main_mod._UPDATE_RECOVERY_OWNERSHIP_COMMITTED = True
        main_mod._register_update_exit_recovery(recover)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        main_mod, "_adopt_transferred_update_recoveries", interrupted_adopt
    )
    monkeypatch.setattr(
        update_receipt, "begin_update_receipt", lambda: events.append("begin")
    )
    monkeypatch.setattr(
        update_receipt,
        "record_step",
        lambda *a, **k: events.append("record-step"),
    )
    finalized = []

    def finalize(outcome, *, stop_reason="", **_kwargs):
        events.append("finalize")
        finalized.append((outcome, stop_reason))

    monkeypatch.setattr(update_receipt, "finalize_update_receipt", finalize)
    monkeypatch.setattr(
        update_cmd,
        "_write_gateway_update_exit_code",
        lambda ok: events.append(f"gateway-status:{ok}"),
    )

    args = SimpleNamespace(gateway=True, plan=False, check=False, branch=None)
    with pytest.raises(SystemExit) as excinfo:
        cmd_update(args)

    assert excinfo.value.code == 2
    assert events == [
        "begin",
        "record-step",
        "recover",
        "finalize",
        "gateway-status:False",
    ]
    assert finalized[0][0] == "failed"
    assert "exit 2" in finalized[0][1]
    if recovery_ok:
        assert "recovery failed" not in finalized[0][1]
    else:
        assert "recovery failed" in finalized[0][1]


def test_pre_accept_adoption_failure_publishes_no_child_status(monkeypatch):
    from hermes_cli import update_cmd, update_receipt

    events = []
    main_mod._UPDATE_EXIT_RECOVERIES.clear()
    main_mod._UPDATE_TRANSFERABLE_RECOVERIES.clear()
    monkeypatch.setattr(main_mod, "_UPDATE_RECOVERY_ACK_COMMITTED", False)
    monkeypatch.setattr(main_mod, "_UPDATE_RECOVERY_OWNERSHIP_COMMITTED", False)
    monkeypatch.setattr(
        main_mod,
        "_adopt_transferred_update_recoveries",
        lambda: (_ for _ in ()).throw(RuntimeError("invalid hand-off")),
    )
    monkeypatch.setattr(
        update_receipt, "begin_update_receipt", lambda: events.append("begin")
    )
    monkeypatch.setattr(
        update_receipt,
        "finalize_update_receipt",
        lambda *a, **k: events.append("finalize"),
    )
    monkeypatch.setattr(
        update_cmd,
        "_write_gateway_update_exit_code",
        lambda ok: events.append(f"gateway-status:{ok}"),
    )

    with pytest.raises(SystemExit) as excinfo:
        cmd_update(SimpleNamespace(gateway=True))

    assert excinfo.value.code == 2
    assert events == []


def test_owned_update_lock_constructor_failure_is_durable(monkeypatch):
    from hermes_cli import update_cmd, update_receipt

    events = []
    main_mod._UPDATE_EXIT_RECOVERIES.clear()
    main_mod._UPDATE_TRANSFERABLE_RECOVERIES.clear()

    def adopt():
        main_mod._UPDATE_RECOVERY_OWNERSHIP_COMMITTED = True
        main_mod._register_update_exit_recovery(
            lambda: events.append("recover") or True
        )
        return True

    monkeypatch.setattr(main_mod, "_adopt_transferred_update_recoveries", adopt)
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr(
        "hermes_cli.update_contract.evaluate_update_admission", lambda _root: None
    )
    io_state = object()
    monkeypatch.setattr(
        main_mod,
        "_install_hangup_protection",
        lambda gateway_mode=False: io_state,
    )
    monkeypatch.setattr(
        main_mod, "_finalize_update_output", lambda state: events.append("restore-stdio")
    )
    monkeypatch.setattr(
        "hermes_cli.update_lock.UpdateLock",
        lambda: (_ for _ in ()).throw(RuntimeError("lock constructor failed")),
    )
    monkeypatch.setattr(
        update_receipt, "begin_update_receipt", lambda: events.append("begin")
    )
    monkeypatch.setattr(update_receipt, "record_step", lambda *a, **k: None)
    monkeypatch.setattr(
        update_receipt,
        "finalize_update_receipt",
        lambda *a, **k: events.append("finalize"),
    )
    monkeypatch.setattr(
        update_cmd,
        "_write_gateway_update_exit_code",
        lambda ok: events.append(f"gateway-status:{ok}"),
    )
    args = SimpleNamespace(plan=False, check=False, gateway=True, branch=None)

    with pytest.raises(RuntimeError, match="lock constructor failed"):
        cmd_update(args)

    assert events == [
        "restore-stdio",
        "begin",
        "recover",
        "finalize",
        "gateway-status:False",
    ]


def test_owned_admission_systemexit_is_durable(monkeypatch):
    from hermes_cli import update_cmd, update_receipt

    events = []
    main_mod._UPDATE_EXIT_RECOVERIES.clear()
    main_mod._UPDATE_TRANSFERABLE_RECOVERIES.clear()

    def adopt():
        main_mod._UPDATE_RECOVERY_OWNERSHIP_COMMITTED = True
        main_mod._register_update_exit_recovery(
            lambda: events.append("recover") or True
        )
        return True

    monkeypatch.setattr(main_mod, "_adopt_transferred_update_recoveries", adopt)
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: False)
    monkeypatch.setattr(
        "hermes_cli.update_contract.evaluate_update_admission",
        lambda _root: (_ for _ in ()).throw(SystemExit(2)),
    )
    monkeypatch.setattr(
        update_receipt, "begin_update_receipt", lambda: events.append("begin")
    )
    monkeypatch.setattr(update_receipt, "record_step", lambda *a, **k: None)
    monkeypatch.setattr(
        update_receipt,
        "finalize_update_receipt",
        lambda *a, **k: events.append("finalize"),
    )
    monkeypatch.setattr(
        update_cmd,
        "_write_gateway_update_exit_code",
        lambda ok: events.append(f"gateway-status:{ok}"),
    )

    with pytest.raises(SystemExit) as excinfo:
        cmd_update(SimpleNamespace(plan=False, check=False, gateway=True, branch=None))

    assert excinfo.value.code == 2
    assert events == ["begin", "recover", "finalize", "gateway-status:False"]


def test_hard_exit_survives_closed_stdout_and_stderr(monkeypatch):
    class ClosedStream:
        @staticmethod
        def flush():
            raise OSError("closed")

    monkeypatch.setattr(main_mod.sys, "stdout", ClosedStream())
    monkeypatch.setattr(main_mod.sys, "stderr", ClosedStream())
    events = _run_cmd_update(monkeypatch, _noop_impl, reexec=True)

    assert events["exit_codes"] == [0]
    assert events["order"][-3:] == ["release", "restore-stdio", "hard-exit"]
