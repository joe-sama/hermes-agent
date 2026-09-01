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
Its reasoning budget is unrestricted and its effort is `xhigh`, the highest
tier accepted by this exact Qwen chat template. Although the generic llama.cpp
binary also advertises `max`, this model template rejects that value.

## Local memory runtime

Hindsight runs from an isolated native-Windows environment at
`G:\LocalAI\hindsight-runtime` and Hermes connects to it over loopback at
`127.0.0.1:9177`. This separation is intentional: Hermes uses MCP 2, while the
pinned Hindsight 0.9.1 server stack requires MCP below 2. The memory database
stays in the existing `hermes` profile (`pg0://hindsight-embed-hermes`), so the
runtime split does not copy or reset memories.

Normal `hermes update` does not modify the isolated runtime, `.hindsight`, or
`.pg0` data. At Windows logon, the gateway Startup wrapper waits for both the
authenticated local model and Hindsight to be healthy before it starts
Telegram. Rerun `scripts/configure-owner-local.ps1` only when this fork changes
the pinned Hindsight version, or after an explicit `hermes gateway install`;
that install command recreates the standard immediate Startup wrapper.

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
