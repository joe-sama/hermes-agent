# Hermes repair checklist — 2026-09-09

Scope: the existing 27B Qwen, native Windows Hermes-managed llama.cpp,
preserved chats, background startup, and verified fixes pushed to this fork.
"Start fresh" means a fresh audit, not another history wipe.

- [x] Reproduce the startup/model-loss problem outside the Codex process tree.
- [x] Unify virtualized and normal Windows data paths into one persistent home.
- [x] Preserve the active SQLite history and retain recoverable old-home copies.
- [x] Replace competing logon entry points with one explicit-home launcher.
- [x] Verify the installed Qwen responds through the managed runtime at 64K context.
- [x] Save the requested 60-second idle VRAM-release setting in configuration and installer defaults.
- [x] Verify the running model releases VRAM after 60 idle seconds and reloads on demand (60.6 seconds observed).
- [x] Move the existing model to NVMe with SHA256 verification and retest cold wake-up: 87.2s to 63.2s during this audit; subsequent idle release 62.4s.
- [ ] Finish repository-wide Python and desktop tests; reproduce and classify failures.
- [x] Run web tests: 39 files / 291 tests passed.
- [x] Run desktop TypeScript checks.
- [x] Run the complete desktop unit suite: 9,285 passed / one 5-second Git fixture timeout; all 18 tests in that file passed after allowing 15 seconds for real Git subprocesses.
- [x] Cover the unified startup wrapper and rerun Windows-owner regressions: 62 tests passed, including component-failure isolation.
- [ ] Verify recent history, memory service, Telegram connection, and quiet startup.
- [ ] Verify fork release-selection fix and GitHub workflow results.
- [ ] Commit and push the source fixes (never private runtime configuration or credentials).
- [ ] Build the desktop again from the final clean commit and verify normal launch.

## Confirmed root cause

The packaged host and normal Windows logon saw different AppData views. The
normal startup view lacked the selected-model configuration and source/model
links, while the packaged-host view held those settings. They now resolve to
one persistent home outside AppData, with explicit-home logon arguments.
Validation uses Task Scheduler under the interactive Windows user, not only
children of the packaged host. A real Windows reboot has **not** been performed.

## CI investigation

The scheduled install/update matrix selected upstream release tags newer than
this fork's ancestry. The picker now limits upgrade baselines to tags reachable
from the checked-out target, with a real temporary-Git regression suite (4
tests passed). The signed-in failure log confirms the newer updater imported
older target files and raised `AttributeError` for
`hermes_cli.main._restart_managed_dashboard_service`. The next GitHub run still
needs verification; selecting a valid baseline is not proof every CI job passes.

## Broader audit fixes

- ACP attachments: native Windows drive paths and file URIs must not be mapped
  into WSL paths. Remote file authorities remain rejected.
- Image references: recognize native drive paths while preserving real-file,
  deduplication, and code-span checks.
- Regression portability: real cross-platform ACP streams, deterministic
  compression timing, native path/environment comparisons, isolated Git line
  endings, and portable token-helper commands.

## Local operational state

- Persistent home: `G:\LocalAI\hermes-home`; both Windows AppData views are
  junctions to this directory. Normal logon explicitly passes this home.
- Model files: `D:\LocalAI\hermes-models` on NVMe. The previous
  `G:\LocalAI\hermes-models` path remains a junction, so existing model IDs,
  sessions, and settings keep working. Both GGUF copies passed SHA256 checks.
- Prior home trees and the SATA model copy remain in dated, recoverable backups.
- Runtime: Hermes-managed Vulkan llama.cpp b10679, the existing 27B Qwen,
  65,536 context, xhigh effort, 2,048 reasoning-token budget, 4,096 response cap.
- Sleep frees model weights/KV; the small worker can remain alive. Measured
  model-worker dedicated VRAM fell from about 17 GB to about 106 MB. Other apps'
  GPU allocations are not part of this measurement.
- There is no promise of instantaneous wake-up: the first request after sleep
  includes loading. No Windows reboot has been performed during this repair.

## Completion rule

An intermediate successful package build is not completion. Record remaining
failures honestly, verify the actual runtime, and rebuild after the final commit.
