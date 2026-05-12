# Hermes Orchestrator-First Update Plan

> **For Hermes:** This is a strategic implementation plan, not a code patch. Use it to guide later subagent/Claude Code/Ralph work. Preserve existing `/busy` semantics; do not globally change busy behavior.

**Goal:** Evolve Hermes from a single foreground agent that becomes user-facing-busy during work into an orchestrator-first workflow where repeated user input is bundled/classified, long work can be delegated to worker lanes, and the main conversation can continue to receive and route input when appropriate.

**Architecture:** Keep `/busy interrupt|queue|steer` intact and add `integrated` as an input-bundling/routing option, not as a replacement for busy. Introduce structured pending inputs, task registry, and eventually asynchronous worker lanes. Existing `delegate_task` is currently synchronous from the parent perspective, so “subagent-first” requires a new background/detached worker layer or Kanban/Claude Code process lane; delegation alone is not enough.

**Tech Stack:** Hermes gateway/Telegram, CLI/TUI busy input handling, `tools/delegate_tool.py`, Kanban plugin, Claude Code subprocess/worktree mode, Python tests, optional TUI TypeScript tests.

---

## 0. Core Product Clarification

### 0.1 Product identity: Hermes as an orchestrating agent

This plan is not merely a `/busy` feature. It is a step toward a distinct operating mode of Hermes:

```text
Hermes as orchestrating agent
  - keeps the user relationship, intent, preferences, and accountability
  - decides when to answer directly vs delegate to Claude Code/subagents/Kanban
  - can actively propose delegation when work is large, risky, or code-heavy
  - can also choose not to delegate when direct handling is faster or more faithful
  - learns the user's preferred delegation style through ongoing conversation
```

This should remain user-shaped rather than hard-coded. Hermes should expose and refine preferences such as:

```text
delegation_style:
  conservative  = ask before delegating except obvious code/test work
  balanced      = delegate large/code-heavy work, summarize when done
  aggressive    = proactively fan out work to Claude Code/subagents with review gates

review_strictness:
  fast          = targeted tests + summary
  standard      = targeted tests + diff review + one reviewer
  high          = spec review + quality review + integration review before import

autonomy_boundary:
  plan_only     = draft plans, wait before implementation
  implement_in_worktree = implement safely in isolated worktree
  import_after_review   = merge/import only after explicit or policy-based approval
```

The product goal is not to force one global behavior. The goal is to let Hermes and the user gradually discover the right operating style per domain, task type, and risk level.

### 0.2 Busy clarification

The user concern is **not** “make Hermes never busy.”

Busy remains valid and user-selectable:

```text
/busy interrupt  # I want to stop/replace the current foreground work
/busy queue      # I want this handled after the current foreground work
/busy steer      # I want this injected into the current run if safe
/busy integrated # I want fragmented follow-ups bundled/classified/routed coherently
```

The real product question is:

> When a user sends repeated Telegram/CLI/TUI inputs during or around a task, how should Hermes decide whether those inputs are a steer, correction, appended requirement, status query, new task, command, media attachment, or fan-out instruction?

The update plan should focus on **orchestrator workflow and input interpretation**, not on eliminating busy state.

---

## 1. Key Architectural Finding

### Existing `delegate_task` does not make the orchestrator always available

Current `delegate_task` is useful, but it is synchronous from the parent agent's point of view:

- The parent tool call waits for child futures/results.
- Batch subagents can run in parallel, but the parent turn still blocks until they finish.
- Therefore, simply “delegating more” does **not** free the main orchestrator to keep handling Telegram turns.

So the correct statement is:

```text
Delegation-first is necessary but insufficient.
The real unlock is: structured input queue + task registry + background/detached worker lanes + synthesis.
```

---

## 2. Desired Operating Model

### 2.1 Busy state by layer

```text
Ingress / gateway:
  accepting     = can receive messages/events
  degraded      = receives but cannot execute normally
  stopped       = gateway unavailable

Main orchestrator:
  idle          = ready to classify/route
  classifying   = briefly interpreting input
  synthesizing  = briefly composing final response
  foreground-busy = actively doing a foreground task; /busy modes matter
  blocked       = needs user decision/approval

Worker lane:
  queued        = assigned but not started
  running       = executing
  steerable     = can accept follow-up at safe boundary
  blocked       = waiting for input/approval/tool
  done/error    = result ready for synthesis/recovery
  cancelled     = stopped or reclaimed
```

Important: main orchestrator **can** still be busy. The point is to avoid making every long task occupy the main conversation if it can safely be moved to a worker lane.

### 2.2 User-facing flow

```text
User: do task X
Hermes main: classifies X
  if short/safe foreground work:
    main handles it, /busy mode applies while working
  if long/multi-step/delegable work:
    main creates task record and worker lane
    main acknowledges and returns availability
Worker: runs task
User sends follow-up:
  main classifies follow-up and attaches/routes it
Worker result arrives:
  main reviews/synthesizes and replies
```

---

## 3. Input Classification Model

Repeated input should be classified into one of these categories:

```text
command       = /busy, /stop, /status, /model, etc.
status        = “어디까지 됐어?”, “결과 언제 나와?”
cancel        = “멈춰”, “그만”, “취소”
correction    = “아니 그 뜻이 아니라…”
append        = “아 맞다 이것도 추가”
steer         = “방금 작업에 이 관점 반영”
new_task      = “그건 됐고 새로 이것도 해줘”
media         = image/audio/document/link attachment
fanout        = “클코로 쪼개서 하나는 리뷰, 하나는 테스트”
ambiguous     = ask, or conservatively queue without mutating worker
```

Initial classifier should be conservative and mostly deterministic:

- Slash commands are hard boundaries.
- Media/attachments are hard boundaries unless explicitly captions/albums.
- Explicit stop/cancel words trigger cancel/reclaim confirmation path.
- Status queries should not disturb active worker.
- Ambiguous follow-ups attach as notes, not as destructive worker steering.
- LLM classification is allowed later only for ambiguous text.

---

## 4. Phased Update Plan

## Execution Method — Compaction, Claude Code Delegation, Review, Import

This work is large enough that Hermes should not attempt one long foreground implementation run.

Use this execution loop before and during implementation:

```text
0. Compress context / write handoff packet
1. Create isolated git worktree for the implementation phase
2. Give Claude Code a narrow task packet with exact files, tests, and acceptance criteria
3. Let Claude Code perform the heavy implementation work in the worktree
4. Hermes reviews the diff, runs targeted tests, and asks independent reviewers for spec/quality review
5. If accepted, import/merge the work back into the main checkout
6. Repeat phase-by-phase; do not let one Claude Code run touch multiple architectural phases
```

### Controller/worker responsibilities

```text
Hermes main orchestrator
  - owns product semantics and user-facing decisions
  - writes/updates the plan and task packets
  - creates rollback points and worktrees
  - reviews Claude Code output before accepting it
  - runs tests and final synthesis
  - keeps Telegram user informed

Claude Code worker
  - performs repo-local implementation in an isolated worktree
  - follows the exact phase/task packet
  - writes tests before or alongside implementation
  - returns changed files, test commands, failures, and rationale
  - does not redefine /busy semantics or broaden scope without approval

Independent reviewers / delegate_task subagents
  - spec compliance review: does the diff implement exactly the requested phase?
  - quality review: regressions, edge cases, maintainability, security
  - final integration review after each phase
```

### Required gates for every phase

```text
Pre-flight gate:
  - current git status captured
  - backup branch/tag or worktree exists
  - task packet written
  - acceptance tests named

Implementation gate:
  - Claude Code changes only the allowed files for the phase
  - tests are added/updated
  - no unrelated formatting or broad refactors

Review gate:
  - Hermes reads the diff
  - spec reviewer passes
  - quality reviewer approves or issues are fixed
  - targeted tests pass

Import gate:
  - changes are merged/imported from worktree only after review
  - main checkout status is verified
  - rollback path remains clear
```

### Suggested worktree pattern

```bash
git worktree add -b feature/orchestrator-integrated-phase-1 /tmp/hermes-orchestrator-phase-1 HEAD
```

Each later phase should use a separate branch/worktree or a clean continuation branch after the previous phase is accepted.

### Claude Code task packet template

```text
You are implementing one narrow Hermes phase in an isolated worktree.

Plan source:
- docs/plans/2026-05-12-hermes-orchestrator-first-update-plan.md

Current phase:
- Phase N: <name>

Do:
- <exact requirements>

Do not:
- change existing /busy interrupt|queue|steer semantics
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
- any open questions
```

---

## Phase 1 — Safe Integrated Input Bundling

**Goal:** Add `/busy integrated` without changing existing modes.

**Scope:** CLI-first, then TUI/gateway later.

**Files likely affected:**

```text
cli.py
hermes_cli/commands.py
tests/cli/test_busy_queue_coalescing.py
```

**Design:**

- Add `integrated` as accepted busy mode.
- While foreground agent is busy, text entered in integrated mode becomes tagged payload:

```python
("integrated_busy", payload)
```

- At drain time, only tagged payloads get integrated wrapper.
- Normal idle messages are not wrapped merely because mode is `integrated`.
- Adjacent text fragments coalesce with `\n\n`.
- Slash commands and media/image payloads remain boundaries.

**Acceptance:**

```bash
python -m pytest tests/cli/test_busy_queue_coalescing.py -q
python -m pytest tests/cli -q
```

---

## Phase 2 — Structured Pending Input Queue

**Goal:** Replace fragile raw-string/single-slot pending handling with structured pending events.

**Why:** Gateway currently has per-session pending-message behavior that can merge/replace depending on path. This is not enough for Telegram-style repeated input.

**Potential new file:**

```text
agent/pending_turn_queue.py
```

**Core type:**

```python
@dataclass
class PendingTurnItem:
    id: str
    session_key: str
    source: str                 # telegram/cli/tui/api
    kind: str                   # text|command|media|attachment|control
    text: str | None
    media_refs: list[str]
    created_at: float
    reply_to: str | None
    task_hint: str | None
    boundary: str               # coalesce|hard|caption|command
```

**Files likely affected:**

```text
gateway/platforms/base.py
gateway/run.py
cli.py
tui_gateway/server.py
ui-tui/src/app/useSubmission.ts
```

**Acceptance:**

- Text fragments preserve order.
- Slash commands are not swallowed.
- Telegram albums/media still merge correctly.
- Queue can contain multiple pending units, not just one slot.

---

## Phase 3 — Task Registry

**Goal:** Give Hermes an explicit record of active user tasks so follow-ups can route to a task, not just to a chat session.

**Potential new file:**

```text
agent/task_registry.py
```

**Task fields:**

```text
task_id
session_key
origin platform/chat/thread/user
status: proposed|queued|running|steerable|blocked|done|error|cancelled
user_goal
active_worker_id
pending_followups
artifacts
created_at/updated_at
```

**Files likely affected:**

```text
hermes_state.py or new SQLite table/module
gateway/run.py
gateway/session.py
cli.py or tui_gateway/server.py for status display
```

**User-facing commands:**

```text
/tasks or /agents     # list active tasks/workers
/status               # include active task summary
/stop <task>          # cancel/reclaim one task
```

**Acceptance:**

- A Telegram-origin task can be listed while worker is running.
- Follow-up can attach to a task id or active task.
- Restart behavior is explicit: either recovered, failed, or marked unknown.

---

## Phase 4 — Background/Detached Worker Lanes

**Goal:** Let long work leave the main foreground turn so the orchestrator can return availability.

**Key finding:** Existing `delegate_task` blocks the parent. Add a detached mode rather than changing default behavior.

**Files likely affected:**

```text
tools/delegate_tool.py
gateway/run.py
gateway/platforms/base.py
gateway/platforms/telegram.py
tests/tools/test_delegate_tool*.py
tests/gateway/test_*delegation*.py
```

**Proposed API:**

```python
delegate_task(..., background=True)
```

Return immediately:

```json
{
  "status": "started",
  "job_id": "...",
  "subagents": [...],
  "delivery": "will notify origin on completion"
}
```

**WorkerLane abstraction:**

```text
WorkerLane.start(task) -> worker_id
WorkerLane.status(worker_id)
WorkerLane.append_followup(worker_id, item) -> accepted|rejected|deferred
WorkerLane.cancel(worker_id)
WorkerLane.result(worker_id)
```

**Initial worker lanes:**

1. `delegate_task(background=True)` lane
2. Claude Code print-mode/background process lane
3. Kanban task lane for durable multi-step work
4. Terminal/background process lane for bounded commands

**Acceptance:**

- Main orchestrator can acknowledge task dispatch and finish its foreground turn quickly.
- Gateway can accept another message while worker runs.
- Worker completion is delivered to original Telegram chat/thread.
- `/status` can show running worker.
- `/stop` can request cancellation/reclaim.

---

## Phase 5 — Follow-up Routing and Steering

**Goal:** Classify repeated input and route it to active task/worker when safe.

**Routing table:**

```text
status query → answer from task registry
cancel       → cancel/reclaim worker or ask confirmation if destructive
append       → attach as pending_followup to task
correction   → attach as high-priority task note; steer if worker supports it
steer        → worker.append_followup if state=steerable; otherwise defer
new task     → create new task registry entry
fanout       → create Kanban/delegate subtasks
ambiguous    → ask or queue conservatively
```

**Important safety rule:** follow-up routing should be append-only at first. Do not mutate worker context destructively unless the worker explicitly supports safe steering.

**Acceptance:**

- “결과 언제 나와?” does not disturb worker.
- “아 맞다 이것도 추가” attaches to active task.
- “그건 빼줘” is tracked as correction and either steered or applied at synthesis.
- “새 작업이야” creates a separate task.

---

## Phase 6 — Synthesis and Review Layer

**Goal:** Main orchestrator reviews worker results before user delivery.

**Synthesis responsibilities:**

- Apply late corrections/append notes.
- Check worker output against original task and follow-ups.
- Summarize clearly to user.
- Attach artifacts/paths/media.
- If worker failed, decide retry/reassign/ask user.

**Acceptance:**

- Worker raw output is not blindly forwarded for complex tasks.
- Hermes tells the user what was done, what changed, what failed, and what needs review.
- Obsidian/file artifacts are verified before being reported.

---

## 5. What Not To Do

Do **not**:

- Replace all busy behavior with integrated mode.
- Make Hermes “pretend” to be always available while the same parent turn is blocked.
- Use synchronous `delegate_task` and claim the orchestrator is free.
- Swallow slash commands into model text.
- Merge media blindly with adjacent text.
- Auto-route ambiguous follow-ups destructively into workers.
- Make background delegation default before status/cancel/delivery are reliable.

---

## 6. Recommended Immediate Next Step

Keep the existing `Integrated Busy Queue` plan as Phase 1. Then add a second implementation plan specifically for **Background Delegation / Worker Lanes**.

Order:

```text
1. Implement `/busy integrated` safely in CLI.
2. Add structured pending queue tests/design for gateway Telegram follow-ups.
3. Prototype `delegate_task(background=True)` or choose Kanban as durable worker lane.
4. Add task registry/status/cancel before making it default.
5. Use Claude Code/Ralph only inside worker lanes, with Hermes as reviewer/synthesizer.
```

This preserves user control over `/busy` while moving Hermes toward a Manus-like operating model.
