# Phase 4 Worker Lane / Detached Execution Substrate Notes

## Summary

Phase 4 adds a small library-level worker-lane substrate in `agent/worker_lanes.py` plus targeted tests in `tests/agent/test_worker_lanes.py`.

The implementation introduces:

- `WorkerSpec`, `WorkerHandle`, and `WorkerResult` JSON-safe shapes.
- `WorkerStatus` lifecycle strings: `queued`, `running`, `done`, `error`, `cancelled`.
- `CancelToken` and `WorkerCancelled` for cooperative cancellation.
- `WorkerLane` protocol.
- `ThreadWorkerLane`, a local in-process lane that runs a caller-supplied runner in a daemon thread.
- `WorkerLaneRegistry`, a small lane manager that routes status/result/cancel/follow-up operations by `worker_id`.
- `link_worker_to_task()`, an optional helper that records worker metadata on Phase 3 `TaskRegistry` tasks.

## PURPOSE-FIT DESIGN RATIONALE

This phase intentionally builds the minimum substrate needed before Hermes can safely grow toward detached/background execution:

- It proves that worker dispatch can return immediately without blocking for completion.
- It gives long-running work a stable `worker_id`, observable lifecycle, and retrievable result/error record.
- It stores late follow-ups as ordered Phase 2 `PendingTurnItem` objects without turning them into model text.
- It links workers to Phase 3 task identity via metadata, while keeping task routing and synthesis in later phases.
- It uses in-process locks for deterministic thread-safe state transitions in the first implementation.

The `ThreadWorkerLane` is deliberately simple and deterministic enough for unit tests. More capable lanes can later wrap Claude Code, terminal background processes, `delegate_task`, Kanban, or Ralph-like focused workers behind the same conceptual API.

## WHAT YOU INTENTIONALLY DID NOT BUILD

This phase does **not** implement:

- Ralph runtime.
- Follow-up classifier.
- Automatic gateway/Telegram routing into active workers.
- User-facing `/tasks`, `/agents`, or `/stop <task>` commands.
- `delegate_task(background=True)` tool schema or public API.
- Claude Code process lane.
- Kanban lane.
- Durable SQLite/multi-process worker database.
- Gateway completion notifications.
- Force-kill semantics for arbitrary external processes.

Cancellation is cooperative: `cancel()` records a request and sets a token; a runner must observe that token to finish as `cancelled`. Uncooperative work may still complete as `done`, with `cancel_requested=True` preserved.

## RALPH/FUTURE FOCUSED-AGENT NOTES

Ralph can later be implemented as a focused worker lane or a higher-level worker runtime that conforms to this substrate:

- Task identity remains in `TaskRegistry`.
- Worker execution state remains in a lane.
- Follow-ups can be attached first, then later routed/steered by Phase 5 policy.
- Hermes main can stay accountable for final synthesis and review in Phase 6.

The current API is intentionally generic: a future `RalphWorkerLane`, `ClaudeCodeWorkerLane`, or `DelegateTaskWorkerLane` can provide the same start/status/follow-up/cancel/result surface without rewriting the task registry.

## Validation

Controller-run targeted validation after fixing two test expectation issues:

```text
/Users/wookim/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/agent/test_worker_lanes.py \
  tests/agent/test_task_registry.py \
  tests/agent/test_pending_turn_queue.py -q

72 passed, 8 warnings
```

```text
/Users/wookim/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/cli/test_busy_queue_coalescing.py \
  tests/cli/test_busy_input_mode_command.py -q

31 passed, 8 warnings
```

```text
/Users/wookim/.hermes/hermes-agent/venv/bin/python -m compileall -q \
  agent/worker_lanes.py agent/task_registry.py agent/pending_turn_queue.py cli.py

git diff --check

passed
```

## Risks / Follow-up

- `ThreadWorkerLane` is in-process only and not durable across process restart.
- Cancellation is cooperative only.
- Snapshot JSON-safety currently validates metadata and follow-up serialization but worker result is coerced to string rather than structured JSON.
- Later phases should decide how completion delivery, task registry persistence, and gateway availability interact.
- Phase 5 should add conservative follow-up routing policy, not direct mutation of running worker contexts.
