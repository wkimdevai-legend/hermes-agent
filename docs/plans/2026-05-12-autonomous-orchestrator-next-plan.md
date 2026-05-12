# Autonomous Orchestrator Next-Step Plan

> **For Hermes:** Continue in autonomous orchestrator workflow. Do not ask for approval for routine implementation, tests, review, docs, local commits, or backups. Ask only at the risk boundaries below.

**Goal:** Move Hermes Base from read-only orchestration observability toward safe, natural follow-up handling while preserving existing gateway/CLI semantics.

**Architecture:** Build incrementally on the committed substrates: `PendingTurnItem` → `TaskRegistry` → `WorkerLaneRegistry` → `FollowupRouter` → `OrchestrationStatusFormatter` → `OrchestrationRuntime` → gated gateway status query. The next work should add append-only follow-up attachment and synthesis contracts without dispatching workers automatically or changing defaults.

**Tech Stack:** Python stdlib, existing Hermes gateway/CLI code, pytest, isolated git worktrees, delegate_task spec/quality reviewers, optional Claude Code only for larger implementation packets.

---

## Current safe baseline

- Latest commit: `b7822ef60 feat(gateway): add gated orchestration status queries`
- Branch: `main`
- Status: local `main` is ahead of upstream; do not push to `origin` without explicit user approval.
- Backup branch/tag created: `backup/orchestrator-status-routing-20260512-200059` / `backup-orchestrator-status-routing-20260512-200059`

## Acceptance criteria for the next milestone

1. A user follow-up can be represented and attached to an existing focused task in an append-only, reversible, session-scoped way.
2. Existing normal agent turns, slash commands, media handling, `/busy`, `/queue`, `/steer`, `/stop`, `/status`, and gateway authorization continue to behave as before.
3. No automatic worker dispatch, LLM classifier, forced cancel, durable DB migration, or default-on gateway routing is introduced.
4. Cross-session privacy remains strict: no task/worker/follow-up detail from another session can appear in a session-scoped reply.
5. Targeted tests, compileall, `git diff --check`, and independent spec/quality review pass before import/commit.

## Risk boundary requiring user approval

Ask before doing any of these:

- Gateway restart or changing live config defaults.
- External push, PR, API side effect, or publishing.
- Destructive command or history rewrite.
- Default-on production gateway routing.
- Existing `/tasks` or `/agents` semantic changes.
- Forced worker/process kill/cancel semantics.
- Durable DB/SQLite migration or multi-process persistence.
- Major architecture direction change such as introducing Ralph runtime/name as a user-facing entity.

## Next milestone: append-only follow-up attachment substrate

### Task 1: Inspect current attachment surfaces

**Objective:** Identify the narrowest safe insertion point for task follow-ups.

**Files:**
- Read: `agent/followup_router.py`
- Read: `agent/task_registry.py`
- Read: `agent/pending_turn_queue.py`
- Read: `agent/orchestration_runtime.py`
- Read: relevant tests under `tests/agent/`

**Verification:** Summarize the exact existing APIs and gaps before editing.

### Task 2: Add a pure helper for session-scoped follow-up attachment

**Objective:** Provide a small library function that takes runtime, session_key, text/event item, and deterministic router result, then appends to exactly one safe task or returns a structured no-op/ambiguous result.

**Likely file:**
- Create or modify: `agent/orchestration_followups.py`
- Test: `tests/agent/test_orchestration_followups.py`

**Rules:**
- Input is already parsed as `PendingTurnItem` or plain text converted to it.
- Append only when deterministic and session-scoped.
- Do not call LLM.
- Do not mutate workers.
- Do not dispatch tasks.
- Do not attach to terminal/done/error/cancelled tasks unless explicitly designed and tested.

### Task 3: Cover privacy and ambiguity tests

**Objective:** Ensure the helper cannot attach follow-ups across sessions or to ambiguous active tasks.

**Tests:**
- zero active tasks → no-op / new-task suggestion, no mutation
- one active task same session → append accepted
- multiple active tasks same session → ambiguous, no mutation
- active task in foreign session → no mutation
- slash command/control item → rejected, no mutation
- media boundary preserved as `PendingTurnItem`, raw excluded from serialization

### Task 4: Optional gateway dry-run/read-only bridge only if safe

**Objective:** If helper is stable, add a feature-flagged dry-run path that classifies/returns what would happen without mutating live task state.

**Default:** off.

**Do not:** enable append mutation in gateway by default in this milestone.

### Task 5: Reviews and import

**Verification commands:**

```bash
venv/bin/python -m pytest \
  tests/agent/test_orchestration_followups.py \
  tests/agent/test_followup_router.py \
  tests/agent/test_task_registry.py \
  tests/agent/test_pending_turn_queue.py \
  tests/gateway/test_session_race_guard.py \
  -q

venv/bin/python -m compileall -q \
  agent/orchestration_followups.py \
  agent/followup_router.py \
  agent/task_registry.py \
  agent/pending_turn_queue.py \
  gateway/run.py

git diff --check
```

Then run two independent reviewers:

- Spec reviewer: scope, session safety, non-goals.
- Quality/security reviewer: privacy, raw serialization, mutation boundaries, regression risks.

Only after both approve:

```bash
git add <files>
git commit -m "feat(agent): add session-scoped follow-up attachment substrate"
```

## What intentionally not to build next

- No Ralph runtime/name layer.
- No background Claude Code lane yet.
- No automatic worker dispatch.
- No LLM ambiguity classifier.
- No `/tasks` or `/agents` semantic change.
- No forced cancel/kill.
- No durable DB migration.
- No default-on gateway mutation.

## Milestone report format

Report only:

- changed files
- test results
- review results
- acceptance criteria status
- remaining risks
- what Woo should check, if anything
