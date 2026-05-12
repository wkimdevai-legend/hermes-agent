# Hermes Orchestrator-First Implementation Handoff

> Purpose: compact handoff packet before implementation. Use this as the stable context for Claude Code / subagents / future Hermes turns.

## User intent

Build Hermes toward a Manus-like, Telegram-first orchestrator experience without breaking current `/busy` semantics.

Important correction from Woo:
- Do **not** suddenly change or remove busy state.
- Main orchestrator can also be busy.
- `/busy interrupt`, `/busy queue`, and `/busy steer` remain valid explicit user choices.
- The core problem is how Hermes handles repeated fragmented input: bundle, classify, route, delegate, review, and synthesize.

## Strategic conclusion

Delegation-first is directionally correct but not sufficient by itself.

Current `delegate_task` is synchronous from the parent perspective, so it does not make the main orchestrator always available. The long-term architecture needs:

```text
structured pending inputs
+ task registry
+ background/detached worker lanes
+ conservative follow-up classification
+ Hermes-owned synthesis/review
```

## Source plans

- `docs/plans/2026-05-12-hermes-orchestrator-first-update-plan.md`
- `docs/plans/2026-05-12-integrated-busy-queue.md`

## Current git baseline

Rollback baseline:

```text
branch: backup/pre-integrated-queue-20260512-082027
tag:    backup-pre-integrated-queue-20260512-082027
commit: 3b513c051 fix(cli): coalesce queued busy inputs
```

Recommended implementation isolation:

```bash
git worktree add -b feature/orchestrator-integrated-phase-1 /tmp/hermes-orchestrator-phase-1 HEAD
```

## Execution model

Do not perform one giant implementation run. Use phase-by-phase Claude Code delegation:

```text
0. Compress context / write handoff packet
1. Create isolated git worktree for the phase
2. Give Claude Code a narrow task packet
3. Claude Code implements and tests in the worktree
4. Hermes reviews diff and targeted tests
5. Independent delegate_task reviewers check spec compliance and quality
6. Import/merge only if accepted
7. Repeat for next phase
```

## Phase order

### Phase 1 — Safe Integrated Input Bundling

Goal:
- Add `/busy integrated` as a safe input interpretation mode.
- Preserve existing `/busy interrupt|queue|steer` behavior.

Likely files:
- `cli.py`
- `hermes_cli/commands.py`
- `tests/cli/test_busy_queue_coalescing.py`

Required tests:

```bash
python -m pytest tests/cli/test_busy_queue_coalescing.py -q
python -m pytest tests/cli -q
```

Acceptance:
- `/busy integrated` is accepted and displayed.
- Existing modes behave unchanged.
- Busy-time fragments are bundled with explicit integrated context.
- Slash commands are not swallowed as plain text.

### Phase 2 — Structured Pending Input Queue

Goal:
- Replace fragile raw string / single-slot pending behavior with structured pending items.

Likely files:
- `gateway/platforms/base.py`
- `gateway/run.py`
- `cli.py`
- `tui_gateway/server.py`
- `ui-tui/src/app/useSubmission.ts`

Acceptance:
- Text fragments preserve order.
- Commands/media/control boundaries remain explicit.
- Queue can hold multiple pending units, not just one slot.

### Phase 3 — Task Registry

Goal:
- Track active user tasks explicitly so follow-ups can route to task IDs, not only chat sessions.

Acceptance:
- Active Telegram-origin task can be listed.
- Follow-ups can attach to active task or task ID.
- Restart behavior is explicit.

### Phase 4 — Background/Detached Worker Lanes

Goal:
- Let long work run outside the main foreground turn.

Key finding:
- Current `delegate_task` blocks parent.
- Add detached/background mode rather than changing default behavior.

Initial lanes:
- `delegate_task(background=True)` lane
- Claude Code print-mode/background process lane
- Kanban task lane
- terminal/background process lane

Acceptance:
- Main orchestrator can acknowledge dispatch and finish quickly.
- Gateway accepts more messages while worker runs.
- Completion is delivered to original Telegram chat/thread.
- `/status` shows running worker.
- `/stop` can request cancellation/reclaim.

### Phase 5 — Follow-up Routing and Steering

Goal:
- Classify repeated input as status/cancel/append/correction/steer/new task/fanout/ambiguous.

Acceptance:
- Ambiguous inputs are not destructively injected.
- Worker steering only occurs at safe boundaries.
- Corrections and append notes are auditable.

### Phase 6 — Synthesis and Review Layer

Goal:
- Hermes stays accountable for final answer quality.

Acceptance:
- Worker outputs are not blindly forwarded.
- Hermes verifies artifacts and tests before claiming success.
- Final user-facing summary includes what changed, tests run, and risks.

## Claude Code task packet template

```text
You are implementing one narrow Hermes phase in an isolated worktree.

Plan source:
- docs/plans/2026-05-12-hermes-orchestrator-first-update-plan.md
- docs/plans/2026-05-12-orchestrator-implementation-handoff.md

Current phase:
- Phase N: <name>

Do:
- <exact requirements>

Do not:
- change existing /busy interrupt|queue|steer semantics
- redefine product semantics
- change gateway delivery behavior outside listed files
- perform broad refactors
- commit credentials or local secrets
- touch unrelated files

Files likely allowed:
- <paths>

Tests required:
- <commands>

Return:
- summary
- changed files
- tests run and results
- known risks
- open questions
```

## Review gates

For every Claude Code output:

1. Hermes reads `git diff`.
2. Run targeted tests.
3. Ask spec reviewer: does it implement exactly the phase?
4. Ask quality reviewer: regressions, edge cases, maintainability, security.
5. Only then import/merge.

## Non-negotiables

```text
- Existing busy semantics remain intact.
- Integrated mode is interpretation/routing, not a global default replacement.
- Do not pretend main orchestrator is available while actually blocked.
- Do not rely on synchronous delegate_task for always-available UX.
- Do not swallow slash commands or media boundaries.
- No credential/token leakage.
```
