# Phase 5 Conservative Follow-up Routing Notes

## Summary

Phase 5 adds a deterministic conservative follow-up routing substrate in `agent/followup_router.py` with targeted tests in `tests/agent/test_followup_router.py`.

The module classifies a single `PendingTurnItem` into a `FollowupDecision` and, when explicitly routed with a `TaskRegistry` / optional `WorkerLaneRegistry`, performs one safe mutation:

- attach plain follow-up to the sole active task
- answer status without mutation
- request cooperative worker cancellation when unambiguous
- record correction as append-only note plus follow-up
- create a new focused task when explicitly requested
- defer/reject ambiguous, command, or unsafe cases

## PURPOSE-FIT DESIGN RATIONALE

The purpose is to bridge the Phase 2/3/4 substrates without jumping directly into gateway behavior:

- Phase 2 preserves input units and boundaries.
- Phase 3 tracks focused task identity and state.
- Phase 4 tracks worker execution identity and state.
- Phase 5 adds conservative policy for deciding where a late follow-up belongs.

The router is deterministic and intentionally cautious. It only acts automatically when there is a single safe target or an explicit new-task signal. Ambiguous cases are deferred rather than guessed.

## WHAT YOU INTENTIONALLY DID NOT BUILD

This phase does not implement:

- Ralph runtime.
- LLM/model classifier.
- automatic Telegram/gateway routing.
- `/tasks`, `/agents`, `/stop <task>` slash commands.
- public `delegate_task(background=True)` API.
- worker result delivery to chat.
- forced cancellation/kill semantics.
- live steering into model contexts.
- durable routing database.
- broad CLI/TUI/gateway changes.

## RALPH/FUTURE FOCUSED-AGENT NOTES

A future Ralph/focused-agent layer can call this router before deciding whether to steer, append, split, or ask the user. The current router provides the safe first-pass policy:

- status queries read task/worker state
- cancellation requests are single-target only
- corrections remain append-only
- new-task phrases create separate task identity
- ambiguous input is deferred for a smarter later layer

Phase 6 can connect this to Hermes synthesis/review so worker results are checked against original task plus accumulated follow-ups before user delivery.

## Validation

Controller-run validation:

```text
/Users/wookim/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/agent/test_followup_router.py \
  tests/agent/test_worker_lanes.py \
  tests/agent/test_task_registry.py \
  tests/agent/test_pending_turn_queue.py -q

106 passed, 8 warnings
```

```text
/Users/wookim/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/cli/test_busy_queue_coalescing.py \
  tests/cli/test_busy_input_mode_command.py \
  tests/gateway/test_restart_drain.py \
  tests/gateway/test_session_race_guard.py -q

65 passed, 8 warnings
```

```text
/Users/wookim/.hermes/hermes-agent/venv/bin/python -m compileall -q \
  agent/followup_router.py agent/worker_lanes.py agent/task_registry.py agent/pending_turn_queue.py cli.py gateway/run.py

git diff --check

passed
```

## Risks / Follow-up

- Trigger matching is deterministic and intentionally simple; later phases may add LLM-assisted classification after this safe layer.
- Korean substring triggers can over-match, so dangerous actions remain single-target only and conservative.
- No gateway integration exists yet; production Telegram behavior is unchanged until a later phase wires this router into message handling.
- No user-facing `/tasks` or `/stop` command is present yet.
