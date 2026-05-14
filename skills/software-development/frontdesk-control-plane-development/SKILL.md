---
name: frontdesk-control-plane-development
description: Use when designing, testing, or wiring Hermes frontdesk/orchestrator behavior so STOP/STATUS/STEER/WORKER routing stays separate from /busy integrated queue replay and main Hermes remains an available reviewer/orchestrator.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [frontdesk, orchestration, worker-lanes, control-plane, hermes-agent]
    related_skills: [hermes-agent, test-driven-development, subagent-driven-development, systematic-debugging]
---

# Frontdesk Control-Plane Development

## Overview

Use this skill when building or reviewing Hermes `frontdesk` behavior: a first-class orchestration/control-plane path where Hermes stays available as frontdesk/reviewer while long work goes to worker lanes. The goal is not a persona prompt and not a tweak to `/busy integrated`; the goal is a structural split between control events, main-agent turns, steering, status, and worker dispatch.

The core invariant: **STOP/STATUS/STEER/WORKER decisions are control-plane routing decisions, not text to be queued and replayed into a future model turn.** In particular, stop/cancel input must never be preserved as pending user text that later re-enters the transcript as "additional input".

## When to Use

Use this skill for:

- Implementing or reviewing frontdesk mode, worker-lane MVPs, task registries, control-plane classifiers, or live surface wiring.
- Fixing bugs where `/stop`, "그만", "멈춰", status queries, or steering messages get queued/replayed incorrectly.
- Deciding whether a request should be handled by main Hermes, a worker lane, status surface, or steer callback.
- Connecting frontdesk behavior to CLI, Gateway, or TUI while keeping it opt-in and rollback-safe.
- Writing PRDs/design reviews for Hermes as an always-available orchestrator/reviewer.

Do **not** use this skill for:

- Generic project management; use `kanban-orchestrator` or `kanban-worker` for board workflows.
- Pure code-debugging unrelated to orchestration; use `systematic-debugging`.
- Adding a frontdesk-looking prompt/persona without real runtime capability.

## Vocabulary

- **Main Hermes:** the foreground conversational agent accountable for user intent, review, synthesis, and final response.
- **Frontdesk:** the structural control plane that receives inputs, classifies them, and routes to STOP/STATUS/STEER/WORKER/MAIN.
- **Task:** the logical user objective, recorded in a task registry.
- **Worker:** an execution unit/lane that works on a task asynchronously or detached from the main turn.
- **STEER:** a follow-up intended for a currently running main/worker turn, not a new standalone task.
- **STATUS:** a local state read (`/tasks`, `/agents`, "지금 뭐 하고 있어?") that should not launch a model turn.
- **STOP:** a high-priority control event. It cancels/purges; it is not normal user text.

## Routing Thresholds

Treat a request as a worker-lane candidate when any of these are true:

- Expected to need 3+ tool calls.
- Expected to take more than about 2 minutes.
- Produces an artifact/report/code change/research package.
- Needs background progress, status, cancellation, or later import/review.

Conservative default: if frontdesk mode is not explicitly active, downgrade worker/steer-shaped advice to `MAIN`. This preserves legacy behavior and avoids fake delegation.

## Required Architecture

A real frontdesk path needs all of the following:

1. **Pure classifier/control-plane schema**
   - Deterministic classification.
   - No I/O, no dispatch, no runtime mutation.
   - Closed intent vocabulary: STOP, STATUS, STEER, NEW_TASK_MAIN, NEW_TASK_WORKER, ACK, DUPLICATE, NOISE.

2. **Task registry**
   - Records focused user tasks, status, origin/session, pending follow-ups, active worker linkage, artifacts, and notes.
   - JSON-safe serialization; never serialize raw local passthroughs.

3. **Worker-lane registry**
   - Registers lane names.
   - Starts workers via explicit `WorkerSpec`.
   - Tracks status/result/cancellation by worker id.
   - Links workers back to tasks.

4. **Runtime container**
   - Holds task + worker registries for a specific owner/session.
   - Provides read-only status formatting.
   - Exposes an explicit frontdesk loop only when the caller intentionally uses it.

5. **Surface adapters**
   - CLI/Gateway/TUI should be opt-in initially.
   - Status and cancellation commands must bypass active-session guards.
   - Live wiring must have clear rollback and tests for every surface.

## Minimal Frontdesk Loop

A safe library-level loop should do this:

```text
input text
  -> classify/control-plane decision
  -> if STOP: cancel active tasks/workers; do not queue/replay text
  -> if STATUS: return local overview; do not launch an LLM turn
  -> if STEER: call explicit steer callback only if a turn is in flight
  -> if WORKER: create task, start registered lane, link task<->worker
  -> otherwise: route to MAIN
```

Reference smoke-test shape:

```python
from agent.orchestration_runtime import OrchestrationRuntime
from agent.worker_lanes import ThreadWorkerLane, WorkerSpec, CancelToken

rt = OrchestrationRuntime.create()

def runner(spec: WorkerSpec, token: CancelToken):
    return "done"

rt.worker_registry.register(ThreadWorkerLane(runner=runner, name="thread"))

worker = rt.handle_frontdesk_input(
    "draft a report.md with the audit",
    frontdesk_mode_active=True,
    session_key="smoke-session",
)
assert worker.action == "worker_started"

status = rt.handle_frontdesk_input(
    "지금 뭐 하고 있어?",
    frontdesk_mode_active=True,
    session_key="smoke-session",
)
assert status.action == "status"

stop = rt.handle_frontdesk_input("그만", frontdesk_mode_active=True, session_key="smoke-session")
assert stop.action == "stopped"
```

## Implementation Workflow

Follow strict TDD for behavior changes.

1. **Write failing tests first**
   - STOP cancels active tasks/workers and does not create follow-ups.
   - STATUS returns local runtime overview without launching a worker/model turn.
   - STEER calls a callback only when `main_in_flight=True` and a callback exists.
   - WORKER creates a task, starts a registered lane, and links task to worker.
   - Worker-unavailable path is honest: do not claim a worker started.

2. **Run the RED test**

```bash
cd <worktree>
uv run pytest tests/agent/test_frontdesk_runtime_loop.py -q
```

3. **Implement the smallest library-level change**

Prefer leaf/library modules before touching live surfaces:

- `agent/control_plane.py`
- `agent/task_registry.py`
- `agent/worker_lanes.py`
- `agent/orchestration_runtime.py`
- `agent/orchestration_status.py`

4. **Run targeted regression tests**

```bash
uv run pytest \
  tests/agent/test_frontdesk_runtime_loop.py \
  tests/agent/test_control_plane.py \
  tests/agent/test_orchestration_runtime.py \
  tests/agent/test_worker_lanes.py \
  tests/agent/test_task_registry.py \
  tests/gateway/test_status_command.py \
  tests/gateway/test_command_bypass_active_session.py \
  -q
python -m py_compile agent/orchestration_runtime.py

git diff --check
```

5. **Commit separately by layer**

Recommended commit split:

- `feat: add frontdesk control-plane substrate`
- `feat: add frontdesk worker mvp loop`
- `docs(skills): add frontdesk control-plane development skill`
- Surface wiring commits separately for CLI, Gateway, and TUI if they are non-trivial.

## Live Surface Wiring Rules

Before connecting CLI/Gateway/TUI live paths:

- Keep it opt-in initially (`frontdesk_mode_active=True` only from a clear config/mode gate).
- Do not expose a frontdesk persona unless worker capability actually exists.
- Do not mutate `/busy integrated` drain semantics as a shortcut.
- Do not replace pending queue structures casually; add explicit bridges/adapters.
- Ensure `/stop`, `/tasks`, `/agents`, `/status` bypass active-session guards.
- Provide rollback: one config flag or one isolated branch revert should disable the new path.
- Add tests for each surface before wiring:
  - command bypass while active session exists,
  - no LLM turn for status,
  - STOP not queued,
  - worker-start path creates visible task/worker state,
  - mode-off legacy behavior unchanged.

## Codex/Worker Execution Notes

For Woo's Mac setup, prefer Codex-first for coding/review worker lanes unless quota/auth/capability forces fallback.

Use an explicit trusted workdir when invoking Codex:

```bash
/Users/wookim/.local/bin/codex exec -C /Users/wookim/.hermes/hermes-agent \
  "Smoke test only. Reply exactly: CODEX_OK"
```

Avoid broad home-directory scans in autonomous workers. Use scoped repo commands (`git ls-files`, `find .`, targeted paths) rather than `find /Users/wookim ...`.

## Common Pitfalls

1. **Persona without capability.** A `frontdesk` prompt/skill/mode alone does not make Hermes an orchestrator. Build the control plane and worker lane first.

2. **STOP as text.** If STOP is stored as a pending input, it can replay later as a new task. STOP must be consumed by the control plane.

3. **Status launching work.** Status queries should read local runtime state, not spawn a model turn or worker.

4. **Steer with no target.** STEER only makes sense when a main/worker turn is actually in flight. Otherwise fall back safely.

5. **Fake worker dispatch.** If no lane is registered, say worker unavailable or keep task blocked/cancelled. Do not tell the user a worker is running.

6. **Default-on surface wiring.** Do not silently route all CLI/Gateway/TUI input through an unfinished frontdesk path. Gate it.

7. **Mixing `/busy integrated` with frontdesk.** `/busy integrated` is a queue/input-mode abstraction. Frontdesk is a first-class control plane. Keep them separate.

8. **Unbounded autonomous scans.** Worker agents must not scan the whole user home directory. Bound them to the repo/worktree.

## Verification Checklist

- [ ] Classifier/control-plane is deterministic and mode-gated.
- [ ] STOP cancels active tasks/workers and is not queued/replayed.
- [ ] STATUS returns local task/worker overview without model execution.
- [ ] STEER uses an explicit callback and only when a turn is in flight.
- [ ] WORKER creates task, starts registered lane, and links task to worker.
- [ ] No-worker path is honest and tested.
- [ ] Task/worker snapshots are JSON-safe and do not touch raw passthroughs.
- [ ] CLI/Gateway/TUI wiring remains opt-in until tested.
- [ ] Targeted agent + gateway regression tests pass.
- [ ] `git diff --check` and py_compile pass.
