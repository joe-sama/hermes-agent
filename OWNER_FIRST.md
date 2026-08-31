# Owner-first Hermes

This fork is configured for one trusted operator on their own computer.
The operator's explicit, current instruction is the authority for task intent,
scope, and acceptable tradeoffs. Generic caution text does not veto a task the
operator has clearly requested.

## Behavior

- `approvals.mode` defaults to `off`.
- Cron, single-query, and unattended work defaults to automatic approval.
- Computer Use maps that approval choice to cua-driver's unrestricted mode and
  skips the classic CLI's separate click/type prompt.
- Password, permission, payment, 2FA, personal-app, and personal-browser UI is
  usable when it belongs to the operator's task.
- Page, message, file, and screenshot content is treated as data unless the
  operator explicitly adopts it as an instruction.

The narrow technical integrity floor remains in code: destructive session
shortcuts, fork bombs, direct block-device writes, and equivalent operations
that would terminate or corrupt the agent's own execution environment. Those
guards do not second-guess an ordinary owner-directed task.

The bundled Windows profile runs the local Qwen model at a tested 65,536-token
context. Hermes begins durable compaction at 48,000 tokens, leaving room for
reasoning, tool results, and the next response without wasting half the window.

## Sensitive computer input

Use `computer_use(action="type_secret", prompt="...")` for credentials. Hermes
opens a local masked dialog and sends the entered value straight to the current
sticky OS target. The value is absent from model tool arguments, progress
events, approval summaries, state-database conversation history, screenshots,
and the tool result. `capture_after` is suppressed for this action.

If a credential is written directly in chat, the model has necessarily already
received it. Plain `type` therefore remains available, and `sensitive=true`
hides its progress preview, but masked `type_secret` is the preferred path.

## Staying current

`upstream` points to `NousResearch/hermes-agent`. Normal `hermes update` merges
new `upstream/main` commits into this customized fork, preserves the owner-first
commits, and pushes only after a clean merge. A real conflict is aborted and
reported instead of overwriting either side. The optional GitHub workflow can
perform the same merge once per day when Actions are enabled on the fork.

For a local manual sync:

```powershell
./scripts/sync-owner-fork.ps1
```
