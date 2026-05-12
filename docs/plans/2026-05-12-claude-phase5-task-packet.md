# Claude Code Task Packet — Phase 5 Conservative Follow-up Routing

IMPORTANT: This is a narrow Phase 5 policy/substrate task. Do **not** implement full Ralph runtime, model-based classifier, automatic Telegram/gateway routing, public `/tasks` or `/stop` commands, or worker result delivery. Build a conservative, testable routing layer that can classify and attach follow-ups to existing Phase 3 tasks / Phase 4 workers when it is safe.

## Worktree

```text
/tmp/hermes-orchestrator-phase-5
```

## Baseline

```text
9edae247d feat(agent): add worker lane substrate
```

## Source context

Read these first:

```text
docs/plans/2026-05-12-hermes-orchestrator-first-update-plan.md
docs/plans/2026-05-12-claude-phase3-task-packet.md
docs/plans/2026-05-12-phase3-task-registry-notes.md
docs/plans/2026-05-12-claude-phase4-task-packet.md
docs/plans/2026-05-12-phase4-worker-lanes-notes.md
agent/pending_turn_queue.py
agent/task_registry.py
agent/worker_lanes.py
```

## Product intent

Hermes is becoming a front-desk / concierge / butler orchestrator:

- The user may send multiple short follow-up messages while a focused task or worker is active.
- Hermes should not blindly interrupt, queue, or steer everything.
- Hermes should first decide whether a follow-up is a status query, cancellation request, append, correction, new task, or ambiguous message.
- Conservative routing is safer than over-confident mutation.

Phase 5 should create the first **deterministic conservative routing layer** over the Phase 2/3/4 substrates:

```text
PendingTurnItem -> routing decision -> optional TaskRegistry/WorkerLane mutation
```

## Current phase

Phase 5: **Conservative Follow-up Routing and Task Status UX substrate**.

This phase should be useful as a library module and tests before real gateway/Telegram integration. It may provide helper functions that gateway/CLI can call later, but must not wire them into production gateway flow yet.

## Required scope

Create a small module, likely:

```text
agent/followup_router.py
```

Suggested concepts:

```python
class FollowupIntent:
    STATUS_QUERY = "status_query"
    CANCEL_REQUEST = "cancel_request"
    APPEND = "append"
    CORRECTION = "correction"
    NEW_TASK = "new_task"
    COMMAND = "command"
    MEDIA = "media"
    AMBIGUOUS = "ambiguous"

@dataclass
class FollowupDecision:
    intent: str
    confidence: str              # high|medium|low
    target_task_id: str | None
    target_worker_id: str | None
    action: str                  # answer_status|attach_followup|record_note|request_cancel|create_task|defer|reject
    reason: str
    message: str | None = None   # optional user-facing/status text
```

Implement a conservative router class or functions, e.g.:

```python
class FollowupRouter:
    def classify(item: PendingTurnItem, *, active_tasks: list[FocusedTask], ... ) -> FollowupDecision: ...
    def route(item: PendingTurnItem, *, task_registry: TaskRegistry, worker_registry: WorkerLaneRegistry | None = None, session_key: str | None = None) -> FollowupDecision: ...
```

You may also create small helpers:

- `looks_like_status_query(text)`
- `looks_like_cancel_request(text)`
- `looks_like_new_task(text)`
- `looks_like_correction(text)`
- `select_target_task(...)`
- `format_task_status(...)`

## Conservative routing rules

Use deterministic rules only. No LLM classifier in this phase.

Minimum expected behavior:

1. **Slash commands / command boundary**
   - If `PendingTurnItem.kind` or `boundary` indicates command, classify as `COMMAND` and do not attach to task text.
   - Return action `reject` or `defer` with reason like `command_boundary`.

2. **Media / attachment boundary**
   - If item has media refs or media/attachment kind, classify as `MEDIA`.
   - Only attach if there is exactly one active task in the session; otherwise defer as ambiguous.
   - Preserve the original `PendingTurnItem`; do not convert media to plain text.

3. **Status query**
   - Korean/English examples:
     - `어디까지`, `진행`, `상태`, `언제`, `결과 언제`, `status`, `progress`, `how far`, `done yet`
   - If exactly one active task/worker is targetable, return `STATUS_QUERY` + `answer_status` with a compact status message.
   - Do not mutate task followups for pure status query.

4. **Cancel request**
   - Korean/English examples:
     - `멈춰`, `중단`, `취소`, `그만`, `stop`, `cancel`, `abort`
   - If exactly one active task/worker is targetable, return `CANCEL_REQUEST` + `request_cancel`; if worker registry is supplied, request cancellation on active worker when known.
   - If multiple active tasks exist, return ambiguous/defer; do not cancel multiple tasks automatically.

5. **Correction**
   - Korean/English examples:
     - `빼줘`, `제외`, `수정`, `정정`, `아니`, `not that`, `remove`, `instead`, `correction`
   - If exactly one active task targetable, record as a task note and attach the original item as a pending follow-up if useful.
   - Must be append-only/safe; do not mutate or delete existing task content.

6. **Explicit new task**
   - Korean/English examples:
     - `새 작업`, `별도 작업`, `이건 다른`, `new task`, `separate task`, `another task`
   - Return `NEW_TASK` + `create_task` and create a new task only if helper is explicitly called to route with a registry.
   - New task creation should preserve session/origin if available from `PendingTurnItem`.

7. **Append**
   - Default safe behavior for a plain text follow-up when exactly one active task exists in the session.
   - Attach original `PendingTurnItem` to that task via `TaskRegistry.attach_followup`.

8. **Ambiguous**
   - If there are zero active tasks or multiple active tasks and no explicit task hint, return `AMBIGUOUS` + `defer` with a reason.
   - Do not guess.

## Integration with existing substrates

- Use `TaskRegistry` from Phase 3 for task state and follow-up/note mutation.
- Use `WorkerLaneRegistry` from Phase 4 only for status/cancel if supplied.
- Use `PendingTurnItem` from Phase 2 directly; never serialize/deep-copy `raw`.
- Do not import gateway modules.
- Do not modify `gateway/run.py` or Telegram adapter in this phase.

## Explicit non-goals

Do **not** implement:

- Ralph runtime.
- LLM/model classifier.
- automatic Telegram/gateway routing.
- `/tasks`, `/agents`, `/stop <task>` commands.
- public `delegate_task(background=True)` API.
- worker result delivery to chat.
- forced cancellation/kill semantics.
- multi-worker steering into live model contexts.
- durable routing database.
- broad CLI/TUI UI changes.

## Files likely allowed

Prefer:

```text
agent/followup_router.py
tests/agent/test_followup_router.py
docs/plans/2026-05-12-phase5-followup-routing-notes.md
```

Allowed only if truly needed and small:

```text
agent/task_registry.py
agent/worker_lanes.py
agent/pending_turn_queue.py
```

Avoid editing:

```text
cli.py
gateway/run.py
gateway/platforms/base.py
gateway/platforms/telegram.py
tools/delegate_tool.py
hermes_cli/commands.py
```

## Acceptance criteria

- Status query answers status without attaching a follow-up.
- Cancel request never cancels more than one task/worker automatically.
- Append attaches exactly to one active task when unambiguous.
- Correction records an append-only note/follow-up; does not destructively mutate task goal.
- New task phrases create a separate task when routing with a registry.
- Multiple active tasks produce ambiguous/defer unless an explicit task id/hint is supplied.
- Zero active tasks produce ambiguous/defer for append/correction/status/cancel unless new-task phrase is explicit.
- Command boundaries are not swallowed into task text.
- Media/attachment boundaries are preserved as `PendingTurnItem`, not flattened.
- `PendingTurnItem.raw` is never serialized/deep-copied/touched by routing snapshots.
- No gateway production behavior changes yet.

## Required tests

Add targeted tests for:

```text
tests/agent/test_followup_router.py
```

Test cases should include:

- one active task + plain text -> append
- two active tasks + plain text -> ambiguous/defer
- zero active tasks + plain text -> ambiguous/defer
- status query with active worker -> status message, no followup mutation
- cancel request with one worker -> worker cancellation requested
- cancel request with multiple active tasks -> ambiguous and no cancellation
- correction phrase -> task note/followup append-only
- explicit new task phrase -> new `FocusedTask`
- command boundary -> command/defer/reject, not attached
- media item with one active task -> attached as media `PendingTurnItem`
- media item with multiple tasks -> ambiguous/defer
- raw passthrough object that raises on deepcopy is not touched
- Korean and English trigger examples

Run at minimum:

```bash
/Users/wookim/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/agent/test_followup_router.py \
  tests/agent/test_worker_lanes.py \
  tests/agent/test_task_registry.py \
  tests/agent/test_pending_turn_queue.py -q

/Users/wookim/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/cli/test_busy_queue_coalescing.py \
  tests/cli/test_busy_input_mode_command.py \
  tests/gateway/test_restart_drain.py \
  tests/gateway/test_session_race_guard.py -q

/Users/wookim/.hermes/hermes-agent/venv/bin/python -m compileall -q \
  agent/followup_router.py agent/worker_lanes.py agent/task_registry.py agent/pending_turn_queue.py cli.py gateway/run.py

git diff --check
```

## Required final notes

Create:

```text
docs/plans/2026-05-12-phase5-followup-routing-notes.md
```

Include sections:

```text
## Summary
## PURPOSE-FIT DESIGN RATIONALE
## WHAT YOU INTENTIONALLY DID NOT BUILD
## RALPH/FUTURE FOCUSED-AGENT NOTES
## Validation
## Risks / Follow-up
```

## Claude Code instructions

Use Claude Opus-class model and max effort.

Implement exactly this phase. Do not commit. Do not push. Stop once there is a coherent, testable, reviewable conservative routing substrate. Return:

```text
Summary
Changed files
Tests run + results
Purpose-fit rationale
Intentional non-goals
Ralph/future focused-agent notes
Known risks/questions
```
