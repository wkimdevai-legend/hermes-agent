# Integrated Busy Queue Implementation Plan

> **For Hermes:** Use subagent-driven-development or Claude Code/Ralph-style worker loops to implement this plan task-by-task. Do not implement broad cross-surface refactors until Phase 1 is green.

**Goal:** Add `/busy integrated` so users can send multiple fragmented messages while Hermes is busy, and Hermes will collect, preserve boundaries, and deliver them as one coherent continuation instead of interrupting or treating each fragment as separate turns.

**Architecture:** Phase 1 is CLI-first and rollback-safe: `integrated` behaves like `queue` at capture time, but uses an explicit tagged payload so only messages that actually arrived while busy are wrapped as an integrated continuation. Existing `queue`, `steer`, and `interrupt` behavior must remain unchanged. Phase 2 aligns TUI/gateway semantics and prepares a structured pending-turn queue suitable for future Kanban/Ralph/Manus-like fan-out.

**Tech Stack:** Python 3.11, Hermes CLI (`cli.py`), command registry (`hermes_cli/commands.py`), pytest/unittest, optional later TUI TypeScript and gateway Python changes.

---

## Current State and Rollback Safety

### Current commit baseline

At plan creation time:

```bash
git rev-parse --short HEAD
# 3b513c051
```

Current relevant commits include:

```text
3b513c051 fix(cli): coalesce queued busy inputs
64e1bfab2 fix(cli): suppress classic busy chrome during runs
be0903d16 fix(cli): allow hiding classic status bar by default
169cc2d7d fix(cli): throttle classic wait-loop redraws
37874f63c fix(cli): reduce classic resize scrollback corruption
e5e7f6615 fix: preserve classic CLI scrollback on resize
```

### Backup created before this plan

A rollback branch and tag were created at the current version:

```bash
git branch backup/pre-integrated-queue-20260512-082027
git tag backup-pre-integrated-queue-20260512-082027
```

Rollback commands:

```bash
# Inspect current changes first
git status --short --branch
git log --oneline -n 8

# Return the worktree to the backup commit if needed
git switch main
git reset --hard backup/pre-integrated-queue-20260512-082027

# Or create a recovery branch from the backup without destroying current work
git switch -c recovery/pre-integrated-queue backup/pre-integrated-queue-20260512-082027
```

### Feature branch recommendation

Do not switch the shared repo while an iTerm/TUI Hermes process is using the same checkout unless you intend to affect that process. For implementation, prefer a separate worktree:

```bash
git worktree add -b feature/integrated-busy-queue /tmp/hermes-integrated-busy-queue HEAD
cd /tmp/hermes-integrated-busy-queue
```

If not using worktree, at minimum create a branch pointer without checkout:

```bash
git branch feature/integrated-busy-queue HEAD
```

---

## Manus-like Target Architecture: Integrated Task Queue

`/busy integrated` is only the first input-level feature. The larger target is an **Integrated Task Queue** for Hermes as a Telegram-first orchestrator.

The core idea is that the **main orchestrator must remain available** even while work is running. Hermes should not become “busy” in the user-facing sense just because a long task is active. Instead, the main session should act as a dispatcher/synthesizer that can keep receiving Telegram follow-ups, decide what each follow-up means, and route work to the right execution lane.

### Target roles

```text
Main Hermes orchestrator
  - stays responsive to Telegram/CLI/TUI input
  - receives fragmented follow-ups
  - classifies intent: steer / correction / new task / cancel / command / attachment
  - decides whether to update the active task, enqueue a worker task, or answer directly
  - synthesizes worker outputs and reports back to the user

Worker agents / execution lanes
  - Claude Code sessions for repo/code tasks
  - delegate_task subagents for short parallel investigations
  - Kanban tasks for durable multi-step work
  - cron/no_agent watchdogs for recurring or script-only monitoring
  - browser/file/terminal tools for bounded concrete actions
```

### Telegram-specific motivation

Telegram naturally produces fragmented input:

```text
이거 해줘
아 맞다 이것도 추가해줘
그리고 앞에서 말한 건 빼줘
아니 그건 새 작업이 아니라 방금 작업에 반영해줘
```

In this environment, a simple queue is not enough. Each follow-up may be:

- **Steer:** “방금 작업에 이 관점을 추가해줘.”
- **Correction:** “아니 그 뜻이 아니라 이렇게.”
- **Append:** “아 맞다, 이것도 포함.”
- **New task:** “그건 됐고 다른 것도 해줘.”
- **Command:** `/busy`, `/stop`, `/model`, `/status`.
- **Attachment/media:** image, audio, document, OneDrive link.
- **Fan-out trigger:** “이건 클코로 쪼개고, 하나는 리뷰, 하나는 테스트.”

The orchestrator should classify these rather than blindly interrupting, steering, or queueing.

### Desired execution model

```text
1. User sends task to Hermes over Telegram.
2. Main orchestrator creates/updates an active task context.
3. If the work is long-running, implementation happens in a worker lane:
   - Claude Code
   - delegate_task
   - Kanban worker
   - background process
4. Main orchestrator remains available for follow-up input.
5. Follow-ups are integrated by intent:
   - simple phrasing/correction → update final synthesis notes
   - steer → send to active worker if safe
   - new independent work → create separate queued/kanban task
   - cancel/stop → interrupt or reclaim worker
   - attachment → attach to appropriate task boundary
6. Worker returns result.
7. Orchestrator reviews, corrects, synthesizes, and replies to the user.
```

### Busy state still matters; integrated mode is about interpretation, not eliminating busy

Integrated queue does **not** mean busy state disappears, and it should not globally change existing `/busy` behavior. The main orchestrator can still be temporarily busy when it is doing foreground work, classifying input, synthesizing results, or waiting for a required user decision.

The key distinction is not “busy vs never busy.” The key distinction is:

```text
How should Hermes interpret repeated user input while work is in progress?
```

Current busy modes remain valid:

```text
interrupt = user wants to interrupt/replace the current foreground run
queue     = user wants this handled after the current foreground run
steer     = user wants this injected into the current run if safe
integrated = user is sending fragmented follow-ups that should be bundled, classified, and routed coherently
```

So `/busy integrated` should be framed as an **input interpretation mode**, not as a promise that the orchestrator is never busy.

### Delegation-first workflow: useful, but not sufficient by itself

A more ambitious mode is a **delegation-first workflow**: when work is long-running, Hermes' main session routes it to workers rather than doing everything inline.

```text
Main orchestrator:
  - receives user input
  - classifies repeated follow-ups
  - keeps active task context/registry
  - dispatches work to subagents/workers when appropriate
  - sends steering/correction notes to the right task if safe
  - answers status questions when possible
  - synthesizes final outputs

Workers:
  - Claude Code sessions
  - delegate_task subagents
  - Kanban workers
  - background processes
  - cron/no_agent jobs
```

However, the current `delegate_task` tool is synchronous from the parent agent's perspective: the parent waits for child results before returning. Therefore, simply using `delegate_task` more often does **not** automatically make the main orchestrator continuously available.

The real unlock is:

```text
structured pending inputs
+ task registry
+ background/detached worker lanes
+ conservative follow-up classification
+ final synthesis/review
```

In that model, the user-facing experience can feel Manus-like: the main conversation can acknowledge and route input while worker tasks run. But this requires explicit background worker/job semantics; it should not be implied by ordinary synchronous delegation.

### Layered state model

```text
Ingress / gateway:
  accepting | degraded | stopped

Main orchestrator:
  idle | classifying | foreground-busy | synthesizing | blocked | degraded

Worker/task:
  queued | running | steerable | blocked | done | error | cancelled
```

This preserves the user's ability to choose queue/steer/interrupt for foreground work while enabling richer orchestration when work is delegated.

### Why this differs from current `/busy` modes

Current `/busy` modes are input handling for a single active process:

```text
interrupt = stop current run and handle new input
queue     = wait until current run ends, then handle input as next turn
steer     = inject text into current agent after next tool call
```

Integrated Task Queue is a higher-level orchestration model:

```text
integrated = keep orchestrator responsive, classify follow-ups, route to workers, then synthesize
```

Thus `/busy integrated` should be treated as the **user-facing entry point** into this architecture, not as the whole architecture.

### Implementation layering

```text
Phase 1: Integrated Busy Queue
  - CLI-level `/busy integrated`
  - coalesce fragmented text safely
  - preserve slash/media boundaries
  - use tagged payloads so normal idle prompts are not wrapped

Phase 2: Cross-surface Integrated Queue
  - TUI/gateway accept the same mode
  - Telegram follow-ups use shared queue semantics
  - pending inputs become structured items, not one raw string slot

Phase 3: Orchestrator/Worker Split
  - main Hermes remains available while workers execute
  - long tasks are delegated to Claude Code/delegate_task/Kanban/background lanes
  - follow-ups are classified and routed

Phase 4: Manus-like Task Fabric
  - active task objects with message history
  - task.sendMessage-like follow-up API internally
  - agent_subtask-like worker lanes
  - result synthesis and durable audit trail
```

## Product Semantics

### User-facing behavior

`/busy integrated` should mean:

```text
While Hermes is working, Enter does not interrupt.
Fragments are collected.
Adjacent plain text fragments are merged.
Slash commands remain commands and are not swallowed into the model prompt.
Images/media remain boundaries.
When the current run reaches a safe boundary/finishes, Hermes sends one integrated follow-up prompt.
```

Example:

```text
User while busy:
  이거 말인데
  앞에 말한 Manus 느낌처럼
  그냥 큐가 아니라 통합해서 봐줘

Next turn delivered to model:
  [Integrated follow-up instruction]
  이거 말인데

  앞에 말한 Manus 느낌처럼

  그냥 큐가 아니라 통합해서 봐줘
```

### Distinction from existing modes

- `/busy interrupt`: current default; busy Enter interrupts current run.
- `/busy queue`: do not interrupt; queue next turn. Existing plain-text coalescing remains raw user text.
- `/busy steer`: try mid-run `agent.steer()` after next tool call; fallback to queue if not possible.
- `/busy integrated`: do not interrupt; queue and coalesce, then explicitly frame the result as a continuation to integrate with the previous/current work.

### Why a tagged payload is necessary

Current `_pending_input` stores raw strings or `(text, images)` tuples. At drain time, `process_loop` cannot know whether a raw string was typed while busy or normally while idle. If we simply wrap all prompts when `self.busy_input_mode == "integrated"`, normal idle prompts would also be modified.

Therefore Phase 1 should tag only busy-integrated submissions, e.g.:

```python
("integrated_busy", payload)
```

This keeps origin information without changing default behavior for normal idle messages.

---

## Phase 1 — CLI-only MVP

### Task 1: Add tests for integrated payload tagging

**Objective:** Define the behavior before production code: integrated busy inputs are tagged and later unwrapped/coalesced only if they originated during busy mode.

**Files:**

- Modify: `tests/cli/test_busy_queue_coalescing.py`
- No production changes yet.

**Step 1: Add failing tests**

Add tests similar to:

```python
def test_integrated_busy_payload_is_wrapped_after_coalescing(self):
    cli_mod = _import_cli()
    stub = self._make_cli([
        cli_mod.HermesCLI._make_integrated_busy_payload("두번째"),
        cli_mod.HermesCLI._make_integrated_busy_payload("세번째"),
    ])
    stub.busy_input_mode = "integrated"

    first = cli_mod.HermesCLI._make_integrated_busy_payload("첫번째")
    prepared = cli_mod.HermesCLI._prepare_pending_input_for_turn(stub, first)

    self.assertIn("Additional user input arrived while Hermes was working", prepared)
    self.assertIn("첫번째", prepared)
    self.assertIn("두번째", prepared)
    self.assertIn("세번째", prepared)
    self.assertTrue(stub._pending_input.empty())
```

Also add:

```python
def test_integrated_mode_does_not_wrap_normal_idle_message(self):
    cli_mod = _import_cli()
    stub = self._make_cli([])
    stub.busy_input_mode = "integrated"

    prepared = cli_mod.HermesCLI._prepare_pending_input_for_turn(stub, "정상 입력")

    self.assertEqual(prepared, "정상 입력")
```

**Step 2: Verify RED**

Run:

```bash
python -m pytest tests/cli/test_busy_queue_coalescing.py -q
```

Expected: FAIL because `_make_integrated_busy_payload` / `_prepare_pending_input_for_turn` do not exist.

---

### Task 2: Add CLI helpers for integrated payloads

**Objective:** Implement the minimal helper layer without changing `/busy` yet.

**Files:**

- Modify: `cli.py` near `_coalesce_pending_busy_queue` around line 8461.

**Implementation sketch:**

```python
_INTEGRATED_BUSY_PAYLOAD = "integrated_busy"

@staticmethod
def _make_integrated_busy_payload(payload):
    return (_INTEGRATED_BUSY_PAYLOAD, payload)

@staticmethod
def _is_integrated_busy_payload(item) -> bool:
    return (
        isinstance(item, tuple)
        and len(item) == 2
        and item[0] == _INTEGRATED_BUSY_PAYLOAD
    )

@staticmethod
def _unwrap_integrated_busy_payload(item):
    return item[1] if HermesCLI._is_integrated_busy_payload(item) else item
```

Then add a preparer:

```python
def _format_integrated_busy_input(self, text: str) -> str:
    return (
        "Additional user input arrived while Hermes was working. "
        "Integrate the following follow-up with the task/result you were just producing. "
        "Do not treat it as an unrelated new topic unless the user clearly asks.\n\n"
        f"{text}"
    )

def _prepare_pending_input_for_turn(self, first_input):
    is_integrated = self._is_integrated_busy_payload(first_input)
    first_unwrapped = self._unwrap_integrated_busy_payload(first_input)

    if not is_integrated:
        return self._coalesce_pending_busy_queue(first_input)

    # Coalesce only adjacent integrated text payloads.
    combined = self._coalesce_pending_integrated_busy_queue(first_unwrapped)
    if isinstance(combined, str) and not _looks_like_slash_command(combined):
        return self._format_integrated_busy_input(combined)
    return combined
```

Important: do not reuse `_coalesce_pending_busy_queue` directly on tagged tuples unless it is extended carefully. It currently treats non-strings as boundaries.

**Step 3: Verify GREEN**

Run:

```bash
python -m pytest tests/cli/test_busy_queue_coalescing.py -q
```

Expected: PASS for the new helper tests plus existing tests.

---

### Task 3: Add integrated-specific coalescing with boundaries

**Objective:** Preserve slash commands/media boundaries while coalescing adjacent integrated plain text.

**Files:**

- Modify: `cli.py`
- Modify: `tests/cli/test_busy_queue_coalescing.py`

**Tests to add first:**

```python
def test_integrated_stops_before_slash_command_payload(self):
    cli_mod = _import_cli()
    stub = self._make_cli([
        cli_mod.HermesCLI._make_integrated_busy_payload("추가"),
        "/busy status",
        cli_mod.HermesCLI._make_integrated_busy_payload("나중"),
    ])
    stub.busy_input_mode = "integrated"

    prepared = cli_mod.HermesCLI._prepare_pending_input_for_turn(
        stub,
        cli_mod.HermesCLI._make_integrated_busy_payload("처음"),
    )

    self.assertIn("처음", prepared)
    self.assertIn("추가", prepared)
    self.assertNotIn("/busy status", prepared)
    self.assertEqual(stub._pending_input.get_nowait(), "/busy status")
```

```python
def test_integrated_stops_before_image_payload(self):
    cli_mod = _import_cli()
    image_payload = ("이미지도 봐줘", ["/tmp/a.png"])
    stub = self._make_cli([
        cli_mod.HermesCLI._make_integrated_busy_payload("추가"),
        image_payload,
        cli_mod.HermesCLI._make_integrated_busy_payload("나중"),
    ])
    stub.busy_input_mode = "integrated"

    prepared = cli_mod.HermesCLI._prepare_pending_input_for_turn(
        stub,
        cli_mod.HermesCLI._make_integrated_busy_payload("처음"),
    )

    self.assertIn("처음", prepared)
    self.assertIn("추가", prepared)
    self.assertEqual(stub._pending_input.get_nowait(), image_payload)
```

**Implementation sketch:**

```python
def _coalesce_pending_integrated_busy_queue(self, first_text: str):
    pending = getattr(self, "_pending_input", None)
    if pending is None:
        return first_text

    parts = [first_text]
    restore = []
    while True:
        try:
            item = pending.get_nowait()
        except queue.Empty:
            break

        if self._is_integrated_busy_payload(item):
            unwrapped = self._unwrap_integrated_busy_payload(item)
            if isinstance(unwrapped, str) and unwrapped and not _looks_like_slash_command(unwrapped):
                parts.append(unwrapped)
                continue
            restore.append(unwrapped)
            break

        restore.append(item)
        break

    while True:
        try:
            restore.append(pending.get_nowait())
        except queue.Empty:
            break
    for item in restore:
        pending.put(item)

    return "\n\n".join(parts) if len(parts) > 1 else first_text
```

**Verification:**

```bash
python -m pytest tests/cli/test_busy_queue_coalescing.py -q
```

Expected: PASS.

---

### Task 4: Add `/busy integrated` command support

**Objective:** Make the new mode user-selectable and persistent.

**Files:**

- Modify: `cli.py` around:
  - init normalization lines ~2302-2311
  - `_handle_busy_command` lines ~8504-8545
- Modify: `hermes_cli/commands.py` `/busy` registry entry.
- Modify/add tests.

**Tests to add first:**

```python
def test_busy_command_accepts_integrated(self):
    cli_mod = _import_cli()
    stub = SimpleNamespace(busy_input_mode="interrupt")

    calls = []
    old_save = cli_mod.save_config_value
    old_cprint = cli_mod._cprint
    try:
        cli_mod.save_config_value = lambda key, value: calls.append((key, value)) or True
        cli_mod._cprint = lambda *args, **kwargs: None
        cli_mod.HermesCLI._handle_busy_command(stub, "/busy integrated")
    finally:
        cli_mod.save_config_value = old_save
        cli_mod._cprint = old_cprint

    self.assertEqual(stub.busy_input_mode, "integrated")
    self.assertIn(("display.busy_input_mode", "integrated"), calls)
```

**Implementation details:**

In `__init__`:

```python
if _bim in {"queue", "steer", "integrated"}:
    self.busy_input_mode = _bim
else:
    self.busy_input_mode = "interrupt"
```

In `_handle_busy_command`:

```python
Usage: /busy [queue|integrated|steer|interrupt|status]
```

Allowed set:

```python
{"queue", "integrated", "interrupt", "steer"}
```

Status text:

```python
elif self.busy_input_mode == "integrated":
    _behavior = "collects fragmented follow-ups and integrates them after the current run"
```

Saved behavior:

```python
elif arg == "integrated":
    behavior = "Enter will collect fragmented follow-ups while Hermes is busy and integrate them as one continuation after the current run."
```

In `hermes_cli/commands.py`:

```python
args_hint="[queue|integrated|steer|interrupt|status]",
subcommands=("queue", "integrated", "steer", "interrupt", "status")
```

**Verification:**

```bash
python -m pytest tests/cli/test_busy_queue_coalescing.py -q
python -m pytest tests/hermes_cli/test_commands.py -q  # if present; otherwise targeted command registry tests
```

---

### Task 5: Route busy Enter through integrated mode

**Objective:** When `_agent_running` and mode is `integrated`, queue a tagged integrated payload, not an interrupt and not a raw queue string.

**Files:**

- Modify: `cli.py` around lines ~11395-11423.

**Test first:** Add a helper-level test rather than driving prompt_toolkit if direct UI testing is too expensive. If a suitable test hook does not exist, extract a tiny helper:

```python
def _busy_payload_for_mode(self, text, images):
    payload = (text, images) if images else text
    if self.busy_input_mode == "integrated" and not images and text:
        return self._make_integrated_busy_payload(text), "integrated"
    return payload, self.busy_input_mode
```

Test:

```python
def test_integrated_busy_mode_tags_text_payload(self):
    cli_mod = _import_cli()
    stub = SimpleNamespace(busy_input_mode="integrated")
    payload, mode = cli_mod.HermesCLI._busy_payload_for_mode(stub, "hello", [])
    self.assertTrue(cli_mod.HermesCLI._is_integrated_busy_payload(payload))
    self.assertEqual(mode, "integrated")
```

Image fallback test:

```python
def test_integrated_busy_mode_does_not_tag_image_payload(self):
    cli_mod = _import_cli()
    stub = SimpleNamespace(busy_input_mode="integrated")
    payload, mode = cli_mod.HermesCLI._busy_payload_for_mode(stub, "see image", ["/tmp/a.png"])
    self.assertEqual(payload, ("see image", ["/tmp/a.png"]))
```

**Implementation:**

Around current branch:

```python
if _effective_mode == "queue":
    self._pending_input.put(payload)
```

Change to:

```python
if _effective_mode in {"queue", "integrated"}:
    queued_payload = payload
    if _effective_mode == "integrated" and not images and text:
        queued_payload = self._make_integrated_busy_payload(text)
    self._pending_input.put(queued_payload)
    preview = text if text else f"[{len(images)} image{'s' if len(images) != 1 else ''} attached]"
    if _effective_mode == "integrated" and not images and text:
        _cprint(f"  Will integrate after the current turn: {preview[:80]}{'...' if len(preview) > 80 else ''}")
    else:
        _cprint(f"  Queued for the next turn: {preview[:80]}{'...' if len(preview) > 80 else ''}")
```

**Verification:**

```bash
python -m pytest tests/cli/test_busy_queue_coalescing.py -q
```

---

### Task 6: Wire process loop to prepare integrated payloads

**Objective:** Replace raw coalescing call with a helper that preserves queue mode and only wraps tagged integrated busy payloads.

**Files:**

- Modify: `cli.py` around process loop line where current code calls:

```python
user_input = self._coalesce_pending_busy_queue(user_input)
```

**Implementation:**

Replace with:

```python
user_input = self._prepare_pending_input_for_turn(user_input)
```

**Verification:**

```bash
python -m pytest tests/cli/test_busy_queue_coalescing.py -q
python -m pytest tests/cli -q
```

Expected: no regressions.

---

### Task 7: Manual CLI smoke test

**Objective:** Verify user-facing behavior in a real CLI session without touching gateway.

**Command:**

```bash
hermes -c
```

Inside Hermes:

```text
/busy integrated
```

Then start a long tool task, e.g. ask Hermes to run a long command or inspect a large file. While it is busy, type several fragments:

```text
이거는
Manus처럼
통합해서 봐줘
```

Expected:

- Current run is not interrupted.
- Each fragment shows `Will integrate after the current turn`.
- After current run completes, Hermes processes one integrated continuation.
- Slash command typed while busy, e.g. `/busy status`, is not swallowed into model text.

---

## Phase 2 — TUI and Gateway Alignment

Do this only after Phase 1 is green and reviewed.

### Task 8: Make TUI accept `integrated` config

**Files:**

- `tui_gateway/server.py`
  - `_load_busy_input_mode`
  - setting handler for key `busy`
- `ui-tui/src/app/useSubmission.ts`
  - `handleBusyInput`
  - queue display/ack text

**Behavior:**

- TUI should not downgrade `display.busy_input_mode: integrated` to interrupt.
- Frontend should show integrated queue status distinctly from queue.
- If TUI queue stays frontend-owned, port the text coalescing/integration policy to TypeScript. Longer term, move ownership to backend.

### Task 9: Gateway pending-turn queue design

**Files:**

- `gateway/platforms/base.py`
  - `merge_pending_message_event`
  - pending messages storage
  - drain after session command
- `gateway/run.py`
  - `_queue_or_replace_pending_event`
  - `_handle_active_session_busy_message`
  - quick running-agent guard

**Recommended future abstraction:**

New file:

```text
agent/pending_turn_queue.py
```

Types/functions:

```python
@dataclass
class PendingTurnItem:
    kind: Literal["text", "media", "command", "payload"]
    text: str | None = None
    payload: Any | None = None
    media_urls: list[str] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

class PendingTurnQueue:
    def append(self, item: PendingTurnItem, *, coalesce_text=True, merge_media_albums=True): ...
    def pop_next(self) -> PendingTurnItem | None: ...
    def empty(self) -> bool: ...
```

Gateway policy:

- Adjacent text coalesces with `\n\n`.
- Slash commands are hard boundaries.
- Media/photo album merges preserve album behavior.
- Successful steer does not enqueue.
- Failed steer falls back to integrated queue.
- Drain is sequential and bounded.

---

## Claude Code / Ralph Execution Strategy

### Recommended structure

Use Hermes as accountable orchestrator and Claude Code as implementation/review workers.

#### Worker A — CLI MVP implementer

```bash
claude -p "Implement Phase 1 of docs/plans/2026-05-12-integrated-busy-queue.md using strict TDD. Work only on cli.py, hermes_cli/commands.py, and tests/cli/test_busy_queue_coalescing.py. Do not touch gateway or TUI. Commit to current branch when tests pass." \
  --allowedTools "Read,Edit,Write,Bash" \
  --max-turns 20
```

#### Worker B — review-only critic

```bash
claude -p "Review the integrated busy queue implementation against docs/plans/2026-05-12-integrated-busy-queue.md. Focus on origin tagging, slash command boundaries, image payload boundaries, and regressions to queue/steer/interrupt. Do not modify files; return findings only." \
  --allowedTools "Read,Bash" \
  --max-turns 10
```

#### Worker C — test expander

```bash
claude -p "Add missing tests for /busy integrated edge cases from docs/plans/2026-05-12-integrated-busy-queue.md. Use TDD and do not broaden scope into gateway/TUI." \
  --allowedTools "Read,Edit,Write,Bash" \
  --max-turns 15
```

### Ralph-style loop

Use this if implementation needs several passes:

```text
loop until done:
  1. Claude Code reads plan and current test failures.
  2. It makes one small TDD change.
  3. It runs targeted tests.
  4. It commits.
  5. Hermes reviews diff and decides continue/stop.
```

Progress state should live in:

```text
docs/plans/2026-05-12-integrated-busy-queue.md
.git history
test output
```

not in a long chat context.

---

## Acceptance Criteria

Phase 1 is accepted only if:

- `/busy integrated` appears in `/busy status` usage/help.
- `display.busy_input_mode: integrated` persists and reloads in CLI.
- While busy, integrated mode does not interrupt.
- Adjacent plain text busy fragments become one integrated continuation.
- Normal idle prompts are not wrapped just because mode is integrated.
- Slash commands remain queued as commands/boundaries and are not swallowed into model text.
- Image payloads are not flattened into integrated plain text.
- Existing `/busy queue`, `/busy steer`, and `/busy interrupt` tests still pass.
- Targeted tests pass:

```bash
python -m pytest tests/cli/test_busy_queue_coalescing.py -q
python -m pytest tests/cli -q
```

Phase 2 is accepted only if TUI/gateway tests demonstrate consistent behavior without breaking Telegram media albums or session command follow-ups.

---

## Known Non-goals for Phase 1

- Do not implement full Kanban fan-out yet.
- Do not alter gateway pending-message storage yet.
- Do not move TUI queue ownership to backend yet.
- Do not change default `busy_input_mode` from `interrupt`.
- Do not make `/busy integrated` the default until Woo has tested it manually.

---

## Open Questions for Later

1. Should `/busy integrated` become the recommended default for Woo's CLI/TUI after testing?
2. Should Telegram/gateway always behave like integrated queue during active tasks, or only when configured?
3. Should integrated queue expose a visible queue panel/card in TUI, e.g. `integrated queue (3 fragments)`?
4. Should a long integrated follow-up trigger Kanban/Ralph decomposition automatically, or only when the user asks?
5. Should `/steer` and integrated queue merge if a steer arrives too late and becomes leftover next-turn input?
