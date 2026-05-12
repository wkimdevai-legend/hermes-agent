"""Tests for the conservative follow-up routing layer (orchestrator Phase 5).

These cover ``agent.followup_router`` over the Phase 2/3/4 substrates: the pure
:meth:`FollowupRouter.classify` decisions, the side-effecting
:meth:`FollowupRouter.route` mutations against a real
:class:`~agent.task_registry.TaskRegistry` (and an optional
:class:`~agent.worker_lanes.WorkerLaneRegistry`), the deterministic Korean/English
trigger predicates, target selection / status formatting, and the invariant that
routing never serialises, copies, or otherwise touches
:attr:`~agent.pending_turn_queue.PendingTurnItem.raw`.
"""

import json
import threading

import pytest

from agent.pending_turn_queue import (
    BOUNDARY_COMMAND,
    BOUNDARY_HARD,
    KIND_ATTACHMENT,
    KIND_COMMAND,
    KIND_CONTROL,
    KIND_MEDIA,
    PendingTurnItem,
    from_legacy_cli_payload,
)
from agent.task_registry import (
    STATUS_DONE,
    STATUS_RUNNING,
    TaskRegistry,
)
from agent.worker_lanes import (
    CancelToken,
    ThreadWorkerLane,
    WorkerLaneRegistry,
    WorkerSpec,
    WorkerStatus,
    link_worker_to_task,
)
from agent.followup_router import (
    FollowupAction,
    FollowupConfidence,
    FollowupDecision,
    FollowupIntent,
    FollowupRouter,
    format_task_status,
    looks_like_cancel_request,
    looks_like_correction,
    looks_like_new_task,
    looks_like_status_query,
    select_target_task,
)

TIMEOUT = 5.0


class UncopyableRaw:
    """A ``PendingTurnItem.raw`` payload that explodes if anything deep-copies it."""

    def __deepcopy__(self, memo):  # pragma: no cover - only hit on a regression
        raise AssertionError("PendingTurnItem.raw must not be deep-copied by routing")


def _text(text, **kw):
    kw.setdefault("session_key", "s1")
    return PendingTurnItem(text=text, **kw)


def _router():
    return FollowupRouter()


# --------------------------------------------------------------------------
# FollowupDecision
# --------------------------------------------------------------------------
def test_decision_validates_confidence_and_is_json_safe():
    d = FollowupDecision(
        intent=FollowupIntent.APPEND,
        confidence=FollowupConfidence.MEDIUM,
        action=FollowupAction.ATTACH_FOLLOWUP,
        reason="sole_active_task",
        target_task_id="task-1",
    )
    json.dumps(d.to_dict())
    assert d.to_dict()["target_worker_id"] is None
    assert d.to_dict()["message"] is None

    with pytest.raises(ValueError, match="unknown confidence"):
        FollowupDecision(
            intent=FollowupIntent.APPEND,
            confidence="extremely",
            action=FollowupAction.DEFER,
            reason="x",
        )


# --------------------------------------------------------------------------
# Trigger predicates -- Korean and English
# --------------------------------------------------------------------------
def test_status_query_predicate_korean_and_english():
    for text in (
        "어디까지 됐어?",
        "진행 상황 좀 알려줘",
        "지금 상태가 어때",
        "결과 언제 나와?",
        "what's the status?",
        "any update on this",
        "how far along are we",
        "is it done yet",
    ):
        assert looks_like_status_query(text), text
    for text in ("이것도 추가해줘", "fix the parser", "let's ship it", ""):
        assert not looks_like_status_query(text), text
    assert not looks_like_status_query(None)


def test_cancel_request_predicate_korean_and_english_and_word_boundaries():
    for text in (
        "그만",
        "그만!",
        "그만해줘",
        "멈춰",
        "지금 멈춰줘",
        "이 작업 중단해",
        "취소해 주세요",
        "stop",
        "please stop it",
        "cancel that",
        "abort",
    ):
        assert looks_like_cancel_request(text), text
    # Substrings of unrelated words must not trip the ASCII triggers, and "그만큼"
    # ("that much") must not look like a cancel.
    for text in ("nonstop work please", "stopwatch reading", "그만큼 했으면 됐어", "cancellation policy"):
        # "cancellation policy" -> "cancel" is a prefix, not a whole word: safe.
        assert not looks_like_cancel_request(text), text
    for text in (
        "don't stop, keep going",
        "do not cancel this run",
        "취소하지 마 계속해",
        "중단하지마",
        "그만하지마",
    ):
        assert not looks_like_cancel_request(text), text
    assert not looks_like_cancel_request(None)
    assert not looks_like_cancel_request("")


def test_new_task_predicate_korean_and_english():
    for text in (
        "새 작업: 리팩토링 해줘",
        "이건 별도 작업으로 부탁해",
        "new task - fix the flaky test",
        "let's do this as a separate task",
        "another task: write the changelog",
    ):
        assert looks_like_new_task(text), text
    for text in ("아 맞다 이것도", "keep going", "작업 진행 상황?"):
        assert not looks_like_new_task(text), text
    for text in (
        "no new task, just add this",
        "don't create a new task; append this",
        "do not create a new task",
        "don't create new task; append this",
        "do not make this a separate task",
        "not a separate task, just append this",
        "do not make this another task",
        "새 작업 말고 기존 작업에 추가해",
        "별도 작업 말고 붙여줘",
    ):
        assert not looks_like_new_task(text), text


def test_correction_predicate_korean_and_english():
    for text in (
        "아니 그게 아니라 다른 파일이야",
        "그 부분은 빼줘",
        "B 케이스는 제외하고",
        "not that one, the other module",
        "remove the logging change",
        "do the refactor instead",
        "scratch that",
    ):
        assert looks_like_correction(text), text
    for text in ("이것도 추가", "status please", "ship it"):
        assert not looks_like_correction(text), text


# --------------------------------------------------------------------------
# select_target_task / format_task_status
# --------------------------------------------------------------------------
def test_select_target_task_zero_one_many_and_hint():
    reg = TaskRegistry()
    assert select_target_task([]) is None
    t1 = reg.create_task("a", session_key="s1")
    assert select_target_task([t1]) is t1
    t2 = reg.create_task("b", session_key="s1")
    assert select_target_task([t1, t2]) is None  # ambiguous
    assert select_target_task([t1, t2], task_hint=t2.task_id) is t2
    assert select_target_task([t1, t2], task_hint="task-does-not-exist") is None
    assert select_target_task([t1], task_hint="task-does-not-exist") is None


def test_format_task_status_compact_line():
    reg = TaskRegistry()
    task = reg.create_task("draft the briefing", session_key="s1", status=STATUS_RUNNING)
    reg.attach_followup(task.task_id, PendingTurnItem(text="and add the weather"))
    line = format_task_status(task)
    assert task.task_id in line
    assert "running" in line
    assert "draft the briefing" in line
    assert "1 follow-up queued" in line

    reg.assign_worker(task.task_id, "worker-xyz", worker_kind="thread")
    assert "worker-xyz" in format_task_status(task)


# --------------------------------------------------------------------------
# classify -- command and media boundaries
# --------------------------------------------------------------------------
def test_classify_command_boundary_is_rejected_not_attached():
    router = _router()
    reg = TaskRegistry()
    task = reg.create_task("active task", session_key="s1")

    for item in (
        PendingTurnItem(kind=KIND_COMMAND, text="/stop", boundary=BOUNDARY_COMMAND, session_key="s1"),
        from_legacy_cli_payload("/busy queue", session_key="s1"),
        PendingTurnItem(text="/status", session_key="s1"),  # text that *looks* like a command
    ):
        d = router.classify(item, active_tasks=[task])
        assert d.intent == FollowupIntent.COMMAND
        assert d.action == FollowupAction.REJECT
        assert d.reason == "command_boundary"
        assert d.target_task_id is None

    # route() must not have attached anything either.
    router.route(
        PendingTurnItem(kind=KIND_COMMAND, text="/stop", boundary=BOUNDARY_COMMAND, session_key="s1"),
        task_registry=reg,
    )
    assert task.pending_followups == []


def test_classify_media_with_one_active_task_attaches_as_media_item():
    router = _router()
    reg = TaskRegistry()
    task = reg.create_task("collect inputs", session_key="s1")

    media = from_legacy_cli_payload(("here is the screenshot", ["/tmp/a.png"]), session_key="s1")
    assert media.kind == KIND_MEDIA
    d = router.classify(media, active_tasks=[task])
    assert d.intent == FollowupIntent.MEDIA
    assert d.action == FollowupAction.ATTACH_FOLLOWUP
    assert d.target_task_id == task.task_id

    out = router.route(media, task_registry=reg)
    assert out.action == FollowupAction.ATTACH_FOLLOWUP
    assert len(task.pending_followups) == 1
    stored = task.pending_followups[0]
    assert stored is media  # the original PendingTurnItem, not a flattened copy
    assert stored.kind == KIND_MEDIA
    assert stored.media_refs == ["/tmp/a.png"]


def test_classify_media_with_multiple_active_tasks_defers():
    router = _router()
    reg = TaskRegistry()
    reg.create_task("task one", session_key="s1")
    reg.create_task("task two", session_key="s1")
    media = from_legacy_cli_payload(("", ["/tmp/a.png"]), session_key="s1")

    d = router.classify(media, active_tasks=reg.list_tasks(active_only=True))
    assert d.intent == FollowupIntent.MEDIA
    assert d.action == FollowupAction.DEFER
    assert d.reason == "multiple_active_tasks"

    router.route(media, task_registry=reg)
    for t in reg.list_tasks():
        assert t.pending_followups == []


def test_classify_attachment_and_control_items_route_like_media():
    router = _router()
    reg = TaskRegistry()
    task = reg.create_task("sole task", session_key="s1")

    doc = from_legacy_cli_payload(("", ["/tmp/spec.pdf"]), session_key="s1")
    doc = doc.copy(kind=KIND_ATTACHMENT)
    ctrl = PendingTurnItem(kind=KIND_CONTROL, boundary=BOUNDARY_HARD, session_key="s1")

    for item in (doc, ctrl):
        d = router.classify(item, active_tasks=[task])
        assert d.intent == FollowupIntent.MEDIA
        assert d.action == FollowupAction.ATTACH_FOLLOWUP
        assert d.target_task_id == task.task_id


# --------------------------------------------------------------------------
# classify / route -- plain text append
# --------------------------------------------------------------------------
def test_one_active_task_plus_plain_text_appends():
    router = _router()
    reg = TaskRegistry()
    task = reg.create_task("write the report", session_key="s1")

    item = _text("아 맞다, 결론 부분에 비용 추정도 넣어줘")
    d = router.classify(item, active_tasks=[task])
    assert d.intent == FollowupIntent.APPEND
    assert d.action == FollowupAction.ATTACH_FOLLOWUP
    assert d.confidence == FollowupConfidence.MEDIUM
    assert d.target_task_id == task.task_id

    out = router.route(item, task_registry=reg)
    assert out.action == FollowupAction.ATTACH_FOLLOWUP
    assert task.pending_followups == [item]
    assert task.user_goal == "write the report"  # untouched
    assert task.notes == []


def test_two_active_tasks_plus_plain_text_is_ambiguous_and_does_not_mutate():
    router = _router()
    reg = TaskRegistry()
    a = reg.create_task("task A", session_key="s1")
    b = reg.create_task("task B", session_key="s1")

    item = _text("그리고 이 부분도 신경 써줘")
    d = router.classify(item, active_tasks=[a, b])
    assert d.intent == FollowupIntent.AMBIGUOUS
    assert d.action == FollowupAction.DEFER
    assert d.reason == "multiple_active_tasks"
    assert d.target_task_id is None

    out = router.route(item, task_registry=reg)
    assert out.action == FollowupAction.DEFER
    assert a.pending_followups == []
    assert b.pending_followups == []


def test_zero_active_tasks_plus_plain_text_is_ambiguous():
    router = _router()
    reg = TaskRegistry()
    reg.create_task("finished work", session_key="s1", status=STATUS_DONE)  # terminal -> not active

    item = _text("이것도 같이 부탁해")
    d = router.classify(item, active_tasks=reg.list_tasks(session_key="s1", active_only=True))
    assert d.intent == FollowupIntent.AMBIGUOUS
    assert d.action == FollowupAction.DEFER
    assert d.reason == "no_active_task"

    out = router.route(item, task_registry=reg, session_key="s1")
    assert out.action == FollowupAction.DEFER


def test_task_hint_resolves_among_multiple_active_tasks():
    router = _router()
    reg = TaskRegistry()
    a = reg.create_task("task A", session_key="s1")
    b = reg.create_task("task B", session_key="s1")

    item = _text("여기에 이 메모도 붙여줘", task_hint=b.task_id)
    d = router.classify(item, active_tasks=[a, b])
    assert d.intent == FollowupIntent.APPEND
    assert d.confidence == FollowupConfidence.HIGH
    assert d.target_task_id == b.task_id

    router.route(item, task_registry=reg)
    assert b.pending_followups == [item]
    assert a.pending_followups == []

    # A hint that names no active task -> ambiguous, no mutation.
    stray = _text("붙여줘", task_hint="task-not-here")
    d2 = router.classify(stray, active_tasks=[a, b])
    assert d2.intent == FollowupIntent.AMBIGUOUS
    assert d2.reason == "task_hint_not_active"


# --------------------------------------------------------------------------
# route -- status query (no follow-up mutation)
# --------------------------------------------------------------------------
def test_status_query_with_active_worker_answers_status_without_mutation():
    router = _router()
    reg = TaskRegistry()
    task = reg.create_task("crunch the dataset", session_key="s1", status=STATUS_RUNNING)

    entered, release = threading.Event(), threading.Event()

    def runner(spec, token):  # noqa: ARG001 - signature is the contract
        entered.set()
        release.wait(TIMEOUT)
        return "done"

    lane = ThreadWorkerLane(runner=runner)
    wlr = WorkerLaneRegistry()
    wlr.register(lane)
    handle = wlr.start(WorkerSpec(goal="crunch the dataset", task_id=task.task_id))
    link_worker_to_task(reg, task.task_id, handle)
    assert entered.wait(TIMEOUT)

    item = _text("결과 언제쯤 나와?")
    d = router.classify(item, active_tasks=[task])
    assert d.intent == FollowupIntent.STATUS_QUERY
    assert d.action == FollowupAction.ANSWER_STATUS
    assert d.target_task_id == task.task_id
    assert d.target_worker_id == handle.worker_id
    assert d.message and task.task_id in d.message

    out = router.route(item, task_registry=reg, worker_registry=wlr)
    assert out.action == FollowupAction.ANSWER_STATUS
    assert out.message and handle.worker_id in out.message
    assert WorkerStatus.RUNNING in out.message
    # The status query mutated nothing.
    assert task.pending_followups == []
    assert task.notes == []
    assert task.status == STATUS_RUNNING

    release.set()
    assert lane.wait(handle.worker_id, timeout=TIMEOUT)


def test_status_query_with_multiple_active_tasks_defers():
    router = _router()
    reg = TaskRegistry()
    reg.create_task("task A", session_key="s1", status=STATUS_RUNNING)
    reg.create_task("task B", session_key="s1", status=STATUS_RUNNING)

    item = _text("진행 상황 어때?")
    out = router.route(item, task_registry=reg, session_key="s1")
    assert out.intent == FollowupIntent.AMBIGUOUS
    assert out.action == FollowupAction.DEFER
    assert out.reason == "status_multiple_active_tasks"
    for t in reg.list_tasks():
        assert t.pending_followups == []


# --------------------------------------------------------------------------
# route -- cancel request
# --------------------------------------------------------------------------
def test_cancel_request_with_one_worker_requests_worker_cancellation():
    router = _router()
    reg = TaskRegistry()
    task = reg.create_task("long build", session_key="s1", status=STATUS_RUNNING)

    entered, release = threading.Event(), threading.Event()

    def runner(spec, token: CancelToken):  # noqa: ARG001
        entered.set()
        release.wait(TIMEOUT)
        token.raise_if_cancelled()
        return "unreached"

    lane = ThreadWorkerLane(runner=runner)
    wlr = WorkerLaneRegistry()
    wlr.register(lane)
    handle = wlr.start(WorkerSpec(goal="long build", task_id=task.task_id))
    link_worker_to_task(reg, task.task_id, handle)
    assert entered.wait(TIMEOUT)

    item = _text("아 그냥 멈춰줘")
    d = router.classify(item, active_tasks=[task])
    assert d.intent == FollowupIntent.CANCEL_REQUEST
    assert d.action == FollowupAction.REQUEST_CANCEL
    assert d.target_task_id == task.task_id
    assert d.target_worker_id == handle.worker_id

    out = router.route(item, task_registry=reg, worker_registry=wlr)
    assert out.action == FollowupAction.REQUEST_CANCEL
    assert out.message and handle.worker_id in out.message
    assert wlr.status(handle.worker_id).cancel_requested is True
    # The task itself is left alone (not force-cancelled, no follow-up swallowed).
    assert task.status == STATUS_RUNNING
    assert task.pending_followups == []

    release.set()
    assert lane.wait(handle.worker_id, timeout=TIMEOUT)
    assert lane.result(handle.worker_id).status == WorkerStatus.CANCELLED


def test_cancel_request_with_one_task_but_no_worker_or_registry_just_reports():
    router = _router()
    reg = TaskRegistry()
    task = reg.create_task("foreground work", session_key="s1", status=STATUS_RUNNING)

    item = _text("그만해도 돼")
    # No worker linked, no worker_registry passed: route returns the intent but
    # mutates nothing.
    out = router.route(item, task_registry=reg)
    assert out.intent == FollowupIntent.CANCEL_REQUEST
    assert out.action == FollowupAction.REQUEST_CANCEL
    assert out.target_task_id == task.task_id
    assert out.target_worker_id is None
    assert out.message is None
    assert task.status == STATUS_RUNNING
    assert task.pending_followups == []


def test_cancel_request_with_multiple_active_tasks_is_ambiguous_and_cancels_nothing():
    router = _router()
    reg = TaskRegistry()
    a = reg.create_task("task A", session_key="s1", status=STATUS_RUNNING)
    b = reg.create_task("task B", session_key="s1", status=STATUS_RUNNING)
    reg.assign_worker(a.task_id, "worker-a")
    reg.assign_worker(b.task_id, "worker-b")

    item = _text("멈춰")
    d = router.classify(item, active_tasks=[a, b])
    assert d.intent == FollowupIntent.AMBIGUOUS
    assert d.action == FollowupAction.DEFER
    assert d.reason == "cancel_multiple_active_tasks"
    assert d.target_task_id is None
    assert d.target_worker_id is None

    # Even with a worker registry available, nothing is cancelled.
    lane = ThreadWorkerLane(runner=lambda s, t: "x")
    wlr = WorkerLaneRegistry()
    wlr.register(lane)
    out = router.route(item, task_registry=reg, worker_registry=wlr)
    assert out.action == FollowupAction.DEFER
    assert a.status == STATUS_RUNNING and b.status == STATUS_RUNNING
    assert a.active_worker_id == "worker-a" and b.active_worker_id == "worker-b"


def test_cancel_request_with_a_stale_unknown_worker_id_is_a_safe_noop():
    router = _router()
    reg = TaskRegistry()
    task = reg.create_task("work", session_key="s1", status=STATUS_RUNNING)
    reg.assign_worker(task.task_id, "worker-ghost")  # not registered anywhere

    lane = ThreadWorkerLane(runner=lambda s, t: "x")
    wlr = WorkerLaneRegistry()
    wlr.register(lane)

    out = router.route(_text("취소해줘"), task_registry=reg, worker_registry=wlr)
    assert out.action == FollowupAction.REQUEST_CANCEL
    assert out.target_worker_id == "worker-ghost"
    assert out.message and "not active" in out.message
    assert task.status == STATUS_RUNNING


def test_negated_cancel_phrases_do_not_request_worker_cancellation():
    router = _router()
    reg = TaskRegistry()
    task = reg.create_task("keep working", session_key="s1", status=STATUS_RUNNING)

    entered, release = threading.Event(), threading.Event()

    def runner(spec, token: CancelToken):  # noqa: ARG001
        entered.set()
        release.wait(TIMEOUT)
        token.raise_if_cancelled()
        return "done"

    lane = ThreadWorkerLane(runner=runner)
    wlr = WorkerLaneRegistry()
    wlr.register(lane)
    handle = wlr.start(WorkerSpec(goal="keep working", task_id=task.task_id))
    link_worker_to_task(reg, task.task_id, handle)
    assert entered.wait(TIMEOUT)

    for body in ("don't stop, keep going", "취소하지 마 계속해"):
        out = router.route(_text(body), task_registry=reg, worker_registry=wlr, session_key="s1")
        assert out.intent != FollowupIntent.CANCEL_REQUEST
        assert out.action != FollowupAction.REQUEST_CANCEL
        assert wlr.status(handle.worker_id).cancel_requested is False

    release.set()
    assert lane.wait(handle.worker_id, timeout=TIMEOUT)


# --------------------------------------------------------------------------
# route -- correction (append-only)
# --------------------------------------------------------------------------
def test_correction_phrase_records_append_only_note_and_followup():
    router = _router()
    reg = TaskRegistry()
    task = reg.create_task("refactor the auth module", session_key="s1", status=STATUS_RUNNING)

    item = _text("아니 그 OAuth 부분은 빼줘, 세션 로직만 손대")
    d = router.classify(item, active_tasks=[task])
    assert d.intent == FollowupIntent.CORRECTION
    assert d.action == FollowupAction.RECORD_NOTE
    assert d.target_task_id == task.task_id

    out = router.route(item, task_registry=reg)
    assert out.action == FollowupAction.RECORD_NOTE
    # Append-only: a note was added AND the item was attached; the goal is intact.
    assert len(task.notes) == 1
    assert task.notes[0].startswith("correction:")
    assert "OAuth" in task.notes[0]
    assert task.pending_followups == [item]
    assert task.user_goal == "refactor the auth module"
    assert task.status == STATUS_RUNNING


def test_correction_with_english_trigger_and_no_text_body():
    router = _router()
    reg = TaskRegistry()
    task = reg.create_task("ship the feature", session_key="s1")

    item = _text("actually no, hold off")
    out = router.route(item, task_registry=reg)
    assert out.intent == FollowupIntent.CORRECTION
    assert task.notes[0].startswith("correction:")
    assert task.pending_followups == [item]


def test_correction_with_multiple_active_tasks_defers():
    router = _router()
    reg = TaskRegistry()
    a = reg.create_task("task A", session_key="s1")
    b = reg.create_task("task B", session_key="s1")
    out = router.route(_text("그건 빼줘"), task_registry=reg, session_key="s1")
    assert out.intent == FollowupIntent.AMBIGUOUS
    assert out.reason == "correction_multiple_active_tasks"
    assert a.notes == [] and b.notes == []
    assert a.pending_followups == [] and b.pending_followups == []


# --------------------------------------------------------------------------
# route -- explicit new task
# --------------------------------------------------------------------------
def test_explicit_new_task_phrase_creates_a_separate_focused_task():
    router = _router()
    reg = TaskRegistry()
    existing = reg.create_task("the current task", session_key="s1", status=STATUS_RUNNING)

    item = PendingTurnItem(text="새 작업: 릴리스 노트 초안 작성해줘", session_key="s1", source="telegram")
    d = router.classify(item, active_tasks=[existing])
    assert d.intent == FollowupIntent.NEW_TASK
    assert d.action == FollowupAction.CREATE_TASK
    assert d.target_task_id is None  # not created yet by classify

    out = router.route(item, task_registry=reg, session_key="s1")
    assert out.intent == FollowupIntent.NEW_TASK
    assert out.target_task_id is not None
    new_task = reg.get_task(out.target_task_id)
    assert new_task is not None
    assert new_task is not existing
    assert new_task.user_goal == "릴리스 노트 초안 작성해줘"  # leading marker peeled off
    assert new_task.session_key == "s1"
    assert new_task.origin.platform == "telegram"
    assert new_task.origin.session_key == "s1"
    # The existing task is untouched and the new one carries no spurious follow-up.
    assert existing.status == STATUS_RUNNING
    assert existing.pending_followups == []
    assert new_task.pending_followups == []
    assert len(reg) == 2


def test_new_task_phrase_works_even_with_zero_existing_tasks():
    router = _router()
    reg = TaskRegistry()
    out = router.route(
        PendingTurnItem(text="separate task: audit the logging config", session_key="s2"),
        task_registry=reg,
        session_key="s2",
    )
    assert out.intent == FollowupIntent.NEW_TASK
    assert out.target_task_id is not None
    assert reg.get_task(out.target_task_id).user_goal == "audit the logging config"


def test_negated_new_task_phrases_append_instead_of_creating_task_when_unambiguous():
    router = _router()
    reg = TaskRegistry()
    existing = reg.create_task("the current task", session_key="s1", status=STATUS_RUNNING)

    for body in (
        "no new task, just add this",
        "don't create new task; append this",
        "not a separate task, just append this",
        "새 작업 말고 기존 작업에 추가해",
    ):
        before = len(reg)
        item = _text(body)
        out = router.route(item, task_registry=reg, session_key="s1")
        assert out.intent != FollowupIntent.NEW_TASK
        assert out.action in {FollowupAction.ATTACH_FOLLOWUP, FollowupAction.RECORD_NOTE}
        assert len(reg) == before
        assert reg.get_task(existing.task_id) is existing

    assert [i.text for i in existing.pending_followups] == [
        "no new task, just add this",
        "don't create new task; append this",
        "not a separate task, just append this",
        "새 작업 말고 기존 작업에 추가해",
    ]


def test_new_task_marker_only_keeps_a_non_empty_goal():
    router = _router()
    reg = TaskRegistry()
    out = router.route(PendingTurnItem(text="새 작업", session_key="s1"), task_registry=reg)
    assert out.intent == FollowupIntent.NEW_TASK
    created = reg.get_task(out.target_task_id)
    assert created.user_goal  # not empty
    assert created.user_goal != "(untitled task)"  # fell back to the marker text


# --------------------------------------------------------------------------
# raw passthrough must never be touched
# --------------------------------------------------------------------------
def test_raw_passthrough_object_that_raises_on_deepcopy_is_not_touched():
    router = _router()
    reg = TaskRegistry()
    task = reg.create_task("guard raw", session_key="s1")

    raw = UncopyableRaw()
    item = PendingTurnItem(text="아 이것도 추가해줘", session_key="s1", raw=raw)
    # An APPEND route stores the item as-is; raw must survive untouched.
    router.route(item, task_registry=reg)
    assert task.pending_followups == [item]
    assert task.pending_followups[0].raw is raw

    # And a CORRECTION route (note + attach) likewise leaves raw alone.
    task2 = reg.create_task("guard raw 2", session_key="s2")
    raw2 = UncopyableRaw()
    item2 = PendingTurnItem(text="아니 그건 빼줘", session_key="s2", raw=raw2)
    router.route(item2, task_registry=reg, session_key="s2")
    assert task2.pending_followups[0] is item2
    assert task2.pending_followups[0].raw is raw2

    # A serialised registry snapshot still drops raw without touching it.
    data = reg.to_dict()
    assert all("raw" not in fu for t in data["tasks"] for fu in t["pending_followups"])
    json.dumps(data)


# --------------------------------------------------------------------------
# session scoping
# --------------------------------------------------------------------------
def test_route_scopes_active_tasks_by_session_key():
    router = _router()
    reg = TaskRegistry()
    s1_task = reg.create_task("session-1 work", session_key="s1", status=STATUS_RUNNING)
    reg.create_task("session-2 work", session_key="s2", status=STATUS_RUNNING)

    # An item carrying session_key="s1" -> only s1's task is a candidate, so the
    # plain-text append is unambiguous even though two tasks are active globally.
    item = _text("여기에 한 줄 더 추가", session_key="s1")
    out = router.route(item, task_registry=reg)
    assert out.action == FollowupAction.ATTACH_FOLLOWUP
    assert out.target_task_id == s1_task.task_id
    assert s1_task.pending_followups == [item]

    # An explicit session_key argument overrides the item's.
    other = _text("로그 레벨도 조정", session_key="s1")
    out2 = router.route(other, task_registry=reg, session_key="s2")
    assert out2.action == FollowupAction.ATTACH_FOLLOWUP
    assert reg.get_task(out2.target_task_id).session_key == "s2"


def test_route_with_no_session_key_uses_global_lone_active_task():
    router = _router()
    reg = TaskRegistry()
    task = reg.create_task("the only task", status=STATUS_RUNNING)  # no session key anywhere
    item = PendingTurnItem(text="추가로 이것도")  # no session key
    out = router.route(item, task_registry=reg)
    assert out.action == FollowupAction.ATTACH_FOLLOWUP
    assert task.pending_followups == [item]


# --------------------------------------------------------------------------
# classify tolerates being called without active_tasks
# --------------------------------------------------------------------------
def test_classify_without_active_tasks_defers_for_text_and_creates_for_new_task():
    router = _router()
    plain = router.classify(PendingTurnItem(text="hello there"))
    assert plain.intent == FollowupIntent.AMBIGUOUS
    assert plain.action == FollowupAction.DEFER
    assert plain.reason == "no_active_task"

    fresh = router.classify(PendingTurnItem(text="new task: do the thing"))
    assert fresh.intent == FollowupIntent.NEW_TASK
    assert fresh.action == FollowupAction.CREATE_TASK
