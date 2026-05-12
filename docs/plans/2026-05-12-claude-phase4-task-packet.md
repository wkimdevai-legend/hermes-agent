# Claude Code Task Packet — Phase 4 Worker Lane / Detached Execution Substrate

IMPORTANT: This is a narrow Phase 4 substrate task. Do **not** broaden into full Ralph runtime, follow-up classifier, automatic Telegram routing, or user-facing task UX. Implement only the smallest coherent, testable foundation for detached/background worker lanes that later phases can connect to the task registry and gateway.

## Worktree

```text
/tmp/hermes-orchestrator-phase-4
```

## Baseline

```text
63ac3fffe feat(agent): add focused task registry substrate
```

## Source context

Read these first:

```text
docs/plans/2026-05-12-hermes-orchestrator-first-update-plan.md
docs/plans/2026-05-12-integrated-busy-queue.md
docs/plans/2026-05-12-claude-phase2-task-packet.md
docs/plans/2026-05-12-claude-phase3-task-packet.md
docs/plans/2026-05-12-phase3-task-registry-notes.md
agent/pending_turn_queue.py
agent/task_registry.py
tools/delegate_tool.py
```

## Product intent

Hermes is evolving toward a front-desk / concierge / butler orchestration model:

- Hermes main remains accountable for user intent, prioritization, review, and synthesis.
- Focused tasks have explicit identity/state via Phase 3 `TaskRegistry`.
- Long-running implementation/research work should eventually run in worker lanes so the foreground orchestrator can acknowledge dispatch and remain available.
- Ralph is a future focused worker/task unit concept. Phase 4 should create substrate that could support a Ralph-like worker later, but must not implement Ralph runtime now.

## Current phase

Phase 4: **Worker Lane / Detached Execution Substrate**.

The goal is to define a small, explicit worker-lane abstraction and at least one local/testable lane implementation that can start, track, cancel, and retrieve results for detached work without blocking the caller until completion.

This phase should be useful even before gateway/TUI integration. It should be a library-level substrate with tests.

## Required scope

Create a small module, likely:

```text
agent/worker_lanes.py
```

Suggested concepts:

```python
class WorkerStatus:
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"

@dataclass
class WorkerSpec:
    goal: str
    context: str | None = None
    task_id: str | None = None
    lane: str = "thread"
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class WorkerHandle:
    worker_id: str
    task_id: str | None
    lane: str
    status: str
    created_at: float
    updated_at: float

@dataclass
class WorkerResult:
    worker_id: str
    status: str
    result: str | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
```

Define a lane protocol or base class like:

```python
class WorkerLane(Protocol):
    name: str
    def start(self, spec: WorkerSpec) -> WorkerHandle: ...
    def status(self, worker_id: str) -> WorkerHandle: ...
    def append_followup(self, worker_id: str, item: PendingTurnItem) -> str: ...
    def cancel(self, worker_id: str) -> bool: ...
    def result(self, worker_id: str) -> WorkerResult | None: ...
```

Implement one purpose-fit local lane for tests, e.g. `ThreadWorkerLane` or `InMemoryWorkerLane`:

- Starts a callable or simple worker function in a background thread.
- Returns immediately after starting.
- Tracks status transitions.
- Stores result/error.
- Supports cancellation request state at least cooperatively.
- Supports append_followup by storing Phase 2 `PendingTurnItem` followups; it does not need to inject them into a running worker yet.
- Uses locks around shared state.
- Provides JSON-safe snapshot methods where useful.

Optionally implement a small registry/manager:

```python
class WorkerLaneRegistry:
    register(lane)
    start(lane_name, spec)
    status(worker_id)
    append_followup(worker_id, item)
    cancel(worker_id)
    result(worker_id)
```

If integrating with Phase 3 `TaskRegistry`, keep it shallow and optional:

- It is acceptable for `WorkerSpec.task_id` / `WorkerHandle.task_id` to link to `FocusedTask.task_id`.
- It is acceptable to provide a helper that records `active_worker_id` / `worker_kind` on a supplied `TaskRegistry`.
- Do not build automatic routing from gateway/CLI into task registry or workers.

## Explicit non-goals

Do **not** implement:

- Ralph runtime.
- Follow-up classifier.
- Automatic Telegram/gateway routing to workers.
- `/tasks`, `/agents`, `/stop <task>` user-facing commands.
- `delegate_task(background=True)` tool schema/user-facing API unless it is a very thin internal stub and fully tested. Prefer library substrate first.
- Claude Code process lane unless it remains small and deterministic in tests. Do not spawn real Claude Code in tests.
- Kanban lane.
- SQLite/multi-process durable worker database.
- Gateway notification delivery on completion.
- Full cancellation/kill semantics for arbitrary external processes.
- Broad refactors of `tools/delegate_tool.py`, `gateway/run.py`, or `cli.py`.

## Files likely allowed

Prefer adding small new files and tests:

```text
agent/worker_lanes.py
tests/agent/test_worker_lanes.py
docs/plans/2026-05-12-phase4-worker-lanes-notes.md
```

Allowed only if truly necessary and small:

```text
agent/task_registry.py
agent/pending_turn_queue.py
```

Avoid editing unless there is a compelling, narrow reason:

```text
tools/delegate_tool.py
gateway/run.py
gateway/platforms/base.py
gateway/platforms/telegram.py
cli.py
```

If you believe a user-facing `delegate_task(background=True)` change is necessary, stop and document the proposed design instead of implementing it broadly.

## Acceptance criteria

- Starting a worker returns quickly without waiting for completion.
- Worker status transitions are observable: queued/running → done/error/cancelled.
- Worker results can be retrieved after completion.
- Worker errors are captured without crashing the caller.
- Cancellation request is represented deterministically; if a worker already finished, cancel is a safe no-op/false.
- Follow-ups can be appended and preserved as `PendingTurnItem` order without being routed into model text.
- Worker handle/result snapshots are JSON-safe and do not serialize local-only raw payloads.
- Optional task registry linkage updates `active_worker_id` / `worker_kind` without starting a classifier or gateway routing system.
- Thread-safety is reasonable for in-process substrate.

## Required tests

Add targeted tests for:

```text
tests/agent/test_worker_lanes.py
```

Test cases should include:

- start returns before worker completes
- successful completion stores result
- raised exception stores error/status=error
- cancel before/during worker records cancellation request/status behavior
- followups preserve order and `PendingTurnItem.raw` is not serialized/touched
- manager/registry routes status/result/cancel to the right lane
- duplicate/unknown worker IDs return clear errors
- optional TaskRegistry linkage records worker metadata on the focused task
- JSON snapshots reject or avoid non-JSON-safe metadata as appropriate
```

Run at minimum:

```bash
/Users/wookim/.hermes/hermes-agent/venv/bin/python -m pytest tests/agent/test_worker_lanes.py tests/agent/test_task_registry.py tests/agent/test_pending_turn_queue.py -q
/Users/wookim/.hermes/hermes-agent/venv/bin/python -m pytest tests/cli/test_busy_queue_coalescing.py tests/cli/test_busy_input_mode_command.py -q
/Users/wookim/.hermes/hermes-agent/venv/bin/python -m compileall -q agent/worker_lanes.py agent/task_registry.py agent/pending_turn_queue.py cli.py
git diff --check
```

If broad tests fail, compare against baseline and distinguish pre-existing environment failures from Phase 4 regressions.

## Required final notes

Create:

```text
docs/plans/2026-05-12-phase4-worker-lanes-notes.md
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

Implement exactly this phase. Do not commit. Do not push. Stop once there is a coherent, testable, reviewable substrate. Return:

```text
Summary
Changed files
Tests run + results
Purpose-fit rationale
Intentional non-goals
Ralph/future focused-agent notes
Known risks/questions
```
