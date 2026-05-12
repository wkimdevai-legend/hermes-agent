"""Service-level coverage for the Hermes orchestration substrate.

There is no single ``agent/orchestration_service.py`` module: the "service" is
the *composition* of the Phase 2-7 leaf modules — :class:`PendingTurnItem` /
:class:`PendingTurnQueue` (input substrate), :class:`TaskRegistry` /
:class:`WorkerLaneRegistry` (state), :class:`FollowupRouter` (the conservative
follow-up classifier+router), and :class:`OrchestrationRuntime` (the live
container + read-only observatory bridge).  These tests exercise that
composition end-to-end with the questions the QA plan cares about:

* When fragmented follow-ups arrive while a single focused task is in flight,
  does the substrate *coalesce/attach* them to that task — rather than degrading
  to "one new task / one answer per fragment"?
* Are slash-command and media boundaries preserved through routing?
* Does a status query stay read-only?
* With no active task, does it *defer* instead of guessing?
* Does the read-only runtime snapshot/overview faithfully reflect the state and
  stay JSON-safe / table-free?

If this file is ever superseded by a real ``orchestration_service`` module, these
behaviours are the contract it must keep.
"""

from __future__ import annotations

import json

from agent.followup_router import (
    FollowupAction,
    FollowupIntent,
    FollowupRouter,
)
from agent.orchestration_runtime import OrchestrationRuntime
from agent.pending_turn_queue import (
    KIND_MEDIA,
    PendingTurnItem,
    from_legacy_cli_payload,
    make_integrated_busy_payload,
)
from agent.task_registry import STATUS_RUNNING


SESSION = "tui:local"


def _busy_text_item(text: str, *, session_key: str = SESSION) -> PendingTurnItem:
    """A plain-text fragment as if captured while Hermes was busy (CLI integrated)."""
    return from_legacy_cli_payload(
        make_integrated_busy_payload(text), session_key=session_key
    )


def _fresh_runtime_with_running_task(goal: str) -> tuple[OrchestrationRuntime, str]:
    rt = OrchestrationRuntime.create()
    task = rt.task_registry.create_task(goal, session_key=SESSION, status=STATUS_RUNNING)
    return rt, task.task_id


# ---------------------------------------------------------------------------
# Fragmented busy-time follow-ups attach to the in-flight task (not N tasks)
# ---------------------------------------------------------------------------
def test_fragmented_busy_followups_attach_to_sole_running_task_not_spawn_new_tasks():
    rt, task_id = _fresh_runtime_with_running_task("20초짜리 상태 확인 테스트")
    router = FollowupRouter()

    fragments = ["첫 번째 후속", "두 번째 후속", "이건 같은 맥락으로 통합되어야 해"]
    decisions = [
        router.route(_busy_text_item(f), task_registry=rt.task_registry, session_key=SESSION)
        for f in fragments
    ]

    # Every fragment is an APPEND/attach to the *same* running task — never a
    # NEW_TASK and never AMBIGUOUS/DEFER.  This is the "coalesce, don't replay
    # sequentially" property at the routing layer.
    assert [d.intent for d in decisions] == [FollowupIntent.APPEND] * 3
    assert [d.action for d in decisions] == [FollowupAction.ATTACH_FOLLOWUP] * 3
    assert {d.target_task_id for d in decisions} == {task_id}

    # Still exactly one task, now carrying all three fragments in order.
    tasks = rt.task_registry.list_tasks(session_key=SESSION, active_only=True)
    assert len(tasks) == 1
    followups = tasks[0].pending_followups
    assert [getattr(f, "text", None) for f in followups] == fragments
    assert all(getattr(f, "origin_busy", False) for f in followups)


def test_slash_command_fragment_is_a_hard_boundary_and_is_not_attached():
    rt, task_id = _fresh_runtime_with_running_task("작업 중")
    router = FollowupRouter()

    # A slash command typed while busy must stay a command, not become task text.
    cmd_item = _busy_text_item("/busy status")
    decision = router.route(cmd_item, task_registry=rt.task_registry, session_key=SESSION)
    assert decision.intent == FollowupIntent.COMMAND
    assert decision.action == FollowupAction.REJECT
    # Nothing attached to the task.
    assert rt.task_registry.get_task(task_id).pending_followups == []


def test_media_fragment_is_preserved_and_attached_not_flattened_to_text():
    rt, task_id = _fresh_runtime_with_running_task("작업 중")
    router = FollowupRouter()

    media_item = PendingTurnItem(
        source="tui",
        kind=KIND_MEDIA,
        text="여기 스크린샷",
        media_refs=["/tmp/shot.png"],
        session_key=SESSION,
    )
    decision = router.route(media_item, task_registry=rt.task_registry, session_key=SESSION)
    assert decision.intent == FollowupIntent.MEDIA
    assert decision.action == FollowupAction.ATTACH_FOLLOWUP
    assert decision.target_task_id == task_id

    attached = rt.task_registry.get_task(task_id).pending_followups
    assert len(attached) == 1
    # The media item is attached as-is — refs intact, not dissolved into prose.
    assert attached[0].kind == KIND_MEDIA
    assert attached[0].media_refs == ["/tmp/shot.png"]


def test_status_query_is_read_only_and_carries_a_message():
    rt, task_id = _fresh_runtime_with_running_task("긴 작업")
    router = FollowupRouter()

    decision = router.route(
        _busy_text_item("어디까지 했어?"), task_registry=rt.task_registry, session_key=SESSION
    )
    assert decision.intent == FollowupIntent.STATUS_QUERY
    assert decision.action == FollowupAction.ANSWER_STATUS
    assert decision.target_task_id == task_id
    assert decision.message  # a human-readable status line
    # Read-only: no follow-up attached, status unchanged.
    task = rt.task_registry.get_task(task_id)
    assert task.pending_followups == []
    assert task.status == STATUS_RUNNING


def test_no_active_task_defers_instead_of_guessing():
    rt = OrchestrationRuntime.create()  # empty board
    router = FollowupRouter()

    decision = router.route(
        _busy_text_item("아무 맥락 없는 한 줄"), task_registry=rt.task_registry, session_key=SESSION
    )
    assert decision.intent == FollowupIntent.AMBIGUOUS
    assert decision.action == FollowupAction.DEFER
    assert decision.target_task_id is None
    assert len(rt.task_registry) == 0


def test_explicit_new_task_phrasing_creates_a_task_even_with_one_running():
    rt, running_id = _fresh_runtime_with_running_task("기존 작업")
    router = FollowupRouter()

    decision = router.route(
        _busy_text_item("새 작업: 로그 파일 분석해줘"),
        task_registry=rt.task_registry,
        session_key=SESSION,
    )
    assert decision.intent == FollowupIntent.NEW_TASK
    assert decision.action == FollowupAction.CREATE_TASK
    assert decision.target_task_id and decision.target_task_id != running_id
    assert len(rt.task_registry.list_tasks(session_key=SESSION, active_only=True)) == 2


# ---------------------------------------------------------------------------
# Read-only runtime observatory reflects the composed state
# ---------------------------------------------------------------------------
def test_runtime_snapshot_and_overview_reflect_attached_followups_and_stay_json_safe():
    rt, task_id = _fresh_runtime_with_running_task("상태 확인 테스트")
    router = FollowupRouter()
    for f in ["첫 번째", "두 번째"]:
        router.route(_busy_text_item(f), task_registry=rt.task_registry, session_key=SESSION)

    snap = rt.snapshot(session_key=SESSION)
    data = snap.to_dict()
    # JSON-safe: round-trips through json without raising.
    json.loads(json.dumps(data))
    assert data["counts"]["tasks_total"] == 1
    assert data["counts"]["tasks_active"] == 1
    assert data["counts"]["followups_pending"] == 2
    # The two queued follow-ups are counted on the task view too.
    task_view = data["tasks"][0]
    assert task_view["task_id"] == task_id
    assert task_view["followups"] == 2

    overview = rt.format_overview(session_key=SESSION)
    assert isinstance(overview, str) and overview.strip()
    # Concise, bullet/label style — never a markdown table.
    assert "|---" not in overview and "| ---" not in overview


def test_empty_runtime_overview_is_graceful_not_an_error():
    rt = OrchestrationRuntime.create()
    text = rt.format_overview()
    assert isinstance(text, str) and text.strip()  # a truthful "nothing here" line


def test_integrated_busy_cli_payload_lifts_to_a_coalescible_busy_text_item():
    # The bridge the CLI's `/busy integrated` mode relies on.
    item = from_legacy_cli_payload(make_integrated_busy_payload("후속 한 줄"), session_key=SESSION)
    assert item.text == "후속 한 줄"
    assert item.origin_busy is True
    assert item.is_coalescible_text()
    # And a slash command wrapped the same way stays a command (hard boundary).
    cmd = from_legacy_cli_payload(make_integrated_busy_payload("/busy status"), session_key=SESSION)
    assert cmd.is_command
    assert not cmd.is_coalescible_text()
