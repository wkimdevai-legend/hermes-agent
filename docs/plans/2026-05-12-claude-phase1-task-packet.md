# Claude Code Task Packet — Phase 1 Safe Integrated Input Bundling

You are implementing one narrow Hermes phase in an isolated git worktree.

## Worktree

```text
/tmp/hermes-orchestrator-phase-1
```

## Source plans

Read these local files for context, but implement only Phase 1:

```text
docs/plans/2026-05-12-hermes-orchestrator-first-update-plan.md
docs/plans/2026-05-12-integrated-busy-queue.md
docs/plans/2026-05-12-orchestrator-implementation-handoff.md
```

## Current phase

Phase 1 — Safe Integrated Input Bundling.

## Product intent

Add `/busy integrated` as a safe input interpretation mode. This is **not** a replacement for existing busy semantics.

Existing modes must remain unchanged:

```text
/busy interrupt
/busy queue
/busy steer
```

New mode:

```text
/busy integrated
```

`integrated` should behave like queue at capture time, but queued busy-time fragments should be explicitly wrapped/tagged so that when they are processed later, Hermes can see they were fragmented follow-ups captured during busy state.

## Do

1. Locate current busy input mode config and command handling.
2. Add `integrated` as an accepted value wherever `/busy` mode is parsed/validated/displayed.
3. Preserve current behavior for `interrupt`, `queue`, and `steer`.
4. Reuse queue capture semantics for `integrated` initially.
5. Ensure that only inputs captured while busy under `integrated` are later wrapped as integrated continuation context.
6. Preserve slash-command/control boundaries. Slash commands must not be swallowed as plain user text.
7. Add/update tests, preferably in:

```text
tests/cli/test_busy_queue_coalescing.py
```

## Do not

- Do not change default busy mode.
- Do not alter gateway/TUI behavior in this phase unless a tiny registry/help text change is unavoidable.
- Do not implement Phase 2 structured pending queue.
- Do not implement task registry, worker lanes, or detached delegation.
- Do not perform broad refactors.
- Do not touch credentials, local env files, or unrelated files.
- Do not commit changes.

## Likely files allowed

```text
cli.py
hermes_cli/commands.py
tests/cli/test_busy_queue_coalescing.py
```

If you need any other file, keep it minimal and explain why.

## Required tests

Run these if possible:

```bash
python -m pytest tests/cli/test_busy_queue_coalescing.py -q
python -m pytest tests/cli -q
```

If the full `tests/cli` subset is too slow or has unrelated failures, report the exact failure and run a narrower relevant subset.

## Acceptance criteria

- `/busy integrated` is accepted by command handling.
- Help/status output can represent `integrated` accurately.
- Existing `interrupt`, `queue`, and `steer` tests continue to pass.
- Integrated busy-time fragments are coalesced/preserved in order.
- The later prompt/continuation clearly indicates the input was captured as integrated follow-up while busy.
- Slash commands remain commands and are not merged into integrated text.
- No unrelated files changed.

## Return format

At the end, report:

```text
SUMMARY:
CHANGED FILES:
TESTS RUN:
RESULTS:
KNOWN RISKS:
OPEN QUESTIONS:
```
