# Owner-local Windows startup and context

`configure-owner-local.ps1` keeps the existing data home and config. Startup
uses `pythonw.exe` and `windows-background-launch.py` to detach the short-lived
PowerShell entry point and each desktop app. The terminal is not a service:
after the launch completes it can exit without stopping Hermes. Output remains
in the configured home's `logs/windows-startup*.log` and `desktop-startup.log`.
The normal Start Menu/Desktop Hermes shortcut still opens the real executable.

For this 24 GB GPU, the owner model uses 64K context, bounded xhigh reasoning,
and a 60-second idle unload. In `config.yaml` the model's entry under
`local_runtime.preset_overrides` has `context-limit: 65536`. This is a ceiling
for both launch planning and automatic context growth, including grants saved
by older versions. Placement is recalculated inside that ceiling; it is not a
raw `ctx-size` override over an already-computed CPU/GPU placement. At the
ceiling Hermes uses its normal context compression. No chat history is deleted.
Removing the optional ceiling restores the normal automatic window ladder.

Hindsight remains separate and persistent. Automatic extraction uses a bounded
512-token reasoning budget and at most one worker retry. Its prefetch waits at
most two seconds for a new memory write, leaving time inside the eight-second
recall deadline to retrieve existing memories. This does not lower chat or
background review reasoning effort. Failed memory jobs remain retryable.
