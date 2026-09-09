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
- [x] Verify the full Linux Python and desktop suites in GitHub CI on aadbb87185 (all required checks passed).
- [ ] Finish the final-commit CI matrix. The broad all-tests-on-Windows diagnostic exposed portability failures; its final output was not captured, so it is not a passing result.
- [x] Run web tests: 39 files / 291 tests passed.
- [x] Run desktop TypeScript checks.
- [x] Run the complete desktop unit suite: 9,285 passed / one 5-second Git fixture timeout; all 18 tests in that file passed after allowing 15 seconds for real Git subprocesses.
- [x] Cover the unified startup wrapper and rerun Windows-owner regressions: 62 tests passed, including component-failure isolation.
- [x] Rerun the combined final regressions: 153 passed, zero failed, 15 OS-specific skips across 10 files (188.2 seconds).
- [ ] Verify recent history, memory service, Telegram connection, and quiet startup.
- [ ] Verify fork release-selection fix and GitHub workflow results.
- [x] Commit and push the initial source fixes (aadbb87185; never private runtime configuration or credentials).
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
from the checked-out target, with a real temporary-Git regression suite (6
tests passed, including annotated remote tags without downloading objects).
The signed-in failure log confirms the newer updater imported
older target files and raised `AttributeError` for
`hermes_cli.main._restart_managed_dashboard_service`. Selecting a valid baseline
is not proof every CI job passes.
The first verification run hit HTTP 429 fetching all upstream tag histories.
The follow-up reads only advertised tag IDs, intersects them with local target
ancestry, and retries transient metadata failures using the existing action.
A live read selected March 12 through August 31 baselines, excluding newer tags.
CI run 34387466528 on aadbb87185 completed successfully, including the complete
Linux Python and desktop suites. On fdae264071, CI run 34388911798 passed every
independent job except a Windows update-progress fixture that emitted no startup
URL within 20 seconds (235 other Windows tests passed). Its startup allowance
is now 60 seconds; the actual live HTTP responsiveness assertions are unchanged.

Install/update run 34388911072 passed release selection and six real upgrade
routes. The four failed routes were stopped at upstream Git fetches by HTTP 429,
before the installers ran. The matrix now passes each canonical release's peeled
commit, verifies it is an ancestor, and reuses those exact objects from the full
local checkout via the sandbox's existing source override. It does not skip the
real installation, upgrade, smoke checks, or final-target assertions. Flag probes
also drain their output instead of producing SIGPIPE under `pipefail`.

New regression coverage exercises release identity, rejection of malformed and
future commit IDs, large-output flag probes, and preservation of Windows location
variables without forwarding credentials. Whole-tree Ruff, shell syntax and all
33 workflow YAML documents pass local checks. Final GitHub results remain pending.

## Broader audit fixes

- ACP attachments: native Windows drive paths and file URIs must not be mapped
  into WSL paths. Remote file authorities remain rejected.
- Image references: recognize native drive paths while preserving real-file,
  deduplication, and code-span checks.
- Regression portability: real cross-platform ACP streams, deterministic
  compression timing, native path/environment comparisons, isolated Git line
  endings, and portable token-helper commands.
- Windows fixtures: close real SQLite handles before temporary-directory cleanup;
  successful desktop swap tests use valid PE fixtures and retain integrity checks.
- Canonical test environment: preserve SYSTEMDRIVE so Windows COM does not create
  a literal `%SystemDrive%` cache tree inside the checkout. Diagnostic artifacts
  were moved recoverably outside the repository, not committed.

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
- Live auxiliary resolution confirms compression uses the same local Qwen through
  `http://127.0.0.1:8081/v1`, with the managed runtime credential present. Cloud
  availability warnings alone do not establish that chat or compression switched.

## Completion rule

An intermediate successful package build is not completion. Record remaining
failures honestly, verify the actual runtime, and rebuild after the final commit.
