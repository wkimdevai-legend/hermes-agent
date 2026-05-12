"""Tests for the worker-lane / detached-execution substrate (orchestrator Phase 4).

These cover the leaf module ``agent.worker_lanes`` in isolation: the
:class:`WorkerSpec` / :class:`WorkerHandle` / :class:`WorkerResult` shapes, the
:class:`ThreadWorkerLane` lifecycle (start returns before completion; success
stores a result; a raised exception stores an error; cooperative cancellation),
follow-up storage and order (with ``PendingTurnItem.raw`` neither serialised nor
touched), the :class:`WorkerLaneRegistry` routing, clear errors for unknown /
duplicate ids, optional :class:`~agent.task_registry.TaskRegistry` linkage, and
JSON-safety of the snapshot path (including rejection of non-JSON-safe metadata).
"""

import json
import threading
import time

import pytest

from agent.pending_turn_queue import KIND_MEDIA, PendingTurnItem, from_legacy_cli_payload
from agent.task_registry import TaskRegistry
from agent import worker_lanes as wl
from agent.worker_lanes import (
    CancelToken,
    FOLLOWUP_DEFERRED,
    FOLLOWUP_REJECTED,
    LANE_THREAD,
    ThreadWorkerLane,
    WorkerCancelled,
    WorkerHandle,
    WorkerLaneRegistry,
    WorkerResult,
    WorkerSpec,
    WorkerStatus,
    link_worker_to_task,
)

TIMEOUT = 5.0


class UncopyableRaw:
    """A ``PendingTurnItem.raw`` payload that explodes if anything deep-copies it."""

    def __deepcopy__(self, memo):  # pragma: no cover - only hit on a regression
        raise AssertionError("PendingTurnItem.raw must not be deep-copied")


def _instant_runner(result="ok"):
    def runner(spec, token):  # noqa: ARG001 - signature is the contract
        return result

    return runner


def _gated_runner(*, entered: threading.Event, release: threading.Event, after=lambda spec, token: "done"):
    """A runner that signals *entered*, parks on *release*, then runs *after*."""

    def runner(spec, token):
        entered.set()
        if not release.wait(TIMEOUT):  # pragma: no cover - would mean a hung test
            raise AssertionError("release event was never set")
        return after(spec, token)

    return runner


# --------------------------------------------------------------------------
# WorkerSpec / WorkerHandle / WorkerResult
# --------------------------------------------------------------------------
def test_worker_spec_defaults_and_roundtrip():
    spec = WorkerSpec(goal="draft the briefing")
    assert spec.context is None
    assert spec.task_id is None
    assert spec.lane == LANE_THREAD
    assert spec.metadata == {}

    full = WorkerSpec(
        goal="g", context="c", task_id="task-1", lane="thread", metadata={"priority": 2}
    )
    d = full.to_dict()
    json.dumps(d)  # JSON-safe
    assert d == {
        "goal": "g",
        "context": "c",
        "task_id": "task-1",
        "lane": "thread",
        "metadata": {"priority": 2},
    }
    assert WorkerSpec.from_dict({**d, "future_key": 1}) == full

    with pytest.raises(TypeError, match="metadata must be a dict"):
        WorkerSpec(goal="g", metadata=["not", "a", "dict"])
    with pytest.raises(TypeError, match="goal must be a string"):
        WorkerSpec(goal=object())
    with pytest.raises(TypeError, match="goal must be a string"):
        WorkerSpec(goal=float("nan"))
    with pytest.raises(TypeError, match="context must be a string or None"):
        WorkerSpec(goal="g", context=object())
    with pytest.raises(TypeError, match="task_id must be a string or None"):
        WorkerSpec(goal="g", task_id=object())
    with pytest.raises(TypeError, match="lane must be a non-empty string"):
        WorkerSpec(goal="g", lane="")
    json.dumps(full.to_dict(), allow_nan=False)


def test_worker_handle_and_result_to_dict_are_json_safe():
    h = WorkerHandle(
        worker_id="worker-1",
        task_id="task-1",
        lane="thread",
        status=WorkerStatus.RUNNING,
        created_at=1.0,
        updated_at=2.0,
        cancel_requested=True,
    )
    assert h.is_terminal is False
    json.dumps(h.to_dict())
    assert h.to_dict()["cancel_requested"] is True

    r = WorkerResult(
        worker_id="worker-1",
        status=WorkerStatus.DONE,
        result="payload",
        started_at=1.0,
        finished_at=3.0,
    )
    json.dumps(r.to_dict())
    assert r.to_dict()["result"] == "payload"
    assert r.to_dict()["error"] is None


# --------------------------------------------------------------------------
# CancelToken
# --------------------------------------------------------------------------
def test_cancel_token_behavior():
    token = CancelToken()
    assert token.cancelled is False
    token.raise_if_cancelled()  # no-op while not cancelled
    assert token.wait(timeout=0) is False

    token.request()
    token.request()  # idempotent
    assert token.cancelled is True
    assert token.wait(timeout=0) is True
    with pytest.raises(WorkerCancelled):
        token.raise_if_cancelled()


# --------------------------------------------------------------------------
# ThreadWorkerLane -- lifecycle
# --------------------------------------------------------------------------
def test_start_returns_before_worker_completes_and_status_progresses():
    entered = threading.Event()
    release = threading.Event()
    lane = ThreadWorkerLane(runner=_gated_runner(entered=entered, release=release))

    t0 = time.monotonic()
    handle = lane.start(WorkerSpec(goal="slow work", task_id="task-1"))
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0  # start() did not block on the runner
    assert isinstance(handle, WorkerHandle)
    assert handle.status == WorkerStatus.QUEUED  # clean snapshot: thread is parked on the lane lock
    assert handle.task_id == "task-1"
    assert handle.lane == LANE_THREAD
    assert lane.result(handle.worker_id) is None  # not terminal yet

    assert entered.wait(TIMEOUT)  # the runner did start on its own
    assert lane.status(handle.worker_id).status == WorkerStatus.RUNNING

    release.set()
    assert lane.wait(handle.worker_id, timeout=TIMEOUT)
    assert lane.status(handle.worker_id).status == WorkerStatus.DONE


def test_successful_completion_stores_result():
    lane = ThreadWorkerLane(runner=_instant_runner("the answer"))
    handle = lane.start(WorkerSpec(goal="quick"))
    assert lane.wait(handle.worker_id, timeout=TIMEOUT)

    res = lane.result(handle.worker_id)
    assert isinstance(res, WorkerResult)
    assert res.status == WorkerStatus.DONE
    assert res.result == "the answer"
    assert res.error is None
    assert res.started_at is not None and res.finished_at is not None
    assert res.finished_at >= res.started_at
    handle2 = lane.status(handle.worker_id)
    assert handle2.status == WorkerStatus.DONE
    assert handle2.is_terminal is True
    assert handle2.cancel_requested is False


def test_non_string_runner_return_is_coerced_to_str():
    lane = ThreadWorkerLane(runner=_instant_runner({"k": "v"}))
    handle = lane.start(WorkerSpec(goal="dict result"))
    assert lane.wait(handle.worker_id, timeout=TIMEOUT)
    assert lane.result(handle.worker_id).result == str({"k": "v"})

    lane2 = ThreadWorkerLane(runner=_instant_runner(None))
    h2 = lane2.start(WorkerSpec(goal="none result"))
    assert lane2.wait(h2.worker_id, timeout=TIMEOUT)
    assert lane2.result(h2.worker_id).result is None


def test_raised_exception_stores_error_and_status_error_without_crashing_caller():
    def boom(spec, token):  # noqa: ARG001
        raise ValueError("kaboom")

    lane = ThreadWorkerLane(runner=boom)
    handle = lane.start(WorkerSpec(goal="will fail"))
    assert lane.wait(handle.worker_id, timeout=TIMEOUT)

    res = lane.result(handle.worker_id)
    assert res.status == WorkerStatus.ERROR
    assert res.result is None
    assert "ValueError" in res.error
    assert "kaboom" in res.error
    # The caller is unscathed: a separate healthy lane can still be used normally.
    healthy_lane = ThreadWorkerLane(runner=_instant_runner("recovered"))
    ok = healthy_lane.start(WorkerSpec(goal="recovers"))
    assert healthy_lane.wait(ok.worker_id, timeout=TIMEOUT)
    assert healthy_lane.result(ok.worker_id).status == WorkerStatus.DONE


# --------------------------------------------------------------------------
# ThreadWorkerLane -- cancellation
# --------------------------------------------------------------------------
def test_cancel_before_and_during_worker_cooperative():
    entered = threading.Event()
    release = threading.Event()
    # The runner parks on *release* before doing anything, then cooperates.
    lane = ThreadWorkerLane(
        runner=_gated_runner(
            entered=entered, release=release, after=lambda spec, token: token.raise_if_cancelled() or "unreached"
        )
    )
    handle = lane.start(WorkerSpec(goal="cancellable"))
    assert entered.wait(TIMEOUT)
    assert lane.status(handle.worker_id).status == WorkerStatus.RUNNING
    assert lane.status(handle.worker_id).cancel_requested is False

    assert lane.cancel(handle.worker_id) is True
    assert lane.status(handle.worker_id).cancel_requested is True

    release.set()
    assert lane.wait(handle.worker_id, timeout=TIMEOUT)
    res = lane.result(handle.worker_id)
    assert res.status == WorkerStatus.CANCELLED
    assert res.result is None
    assert res.error is None
    assert lane.status(handle.worker_id).cancel_requested is True

    # Cancelling a finished worker is a safe no-op.
    assert lane.cancel(handle.worker_id) is False


def test_cancel_after_completion_is_a_safe_noop():
    lane = ThreadWorkerLane(runner=_instant_runner("done"))
    handle = lane.start(WorkerSpec(goal="fast"))
    assert lane.wait(handle.worker_id, timeout=TIMEOUT)
    assert lane.status(handle.worker_id).status == WorkerStatus.DONE

    assert lane.cancel(handle.worker_id) is False
    # Status and result are untouched; cancel_requested stays False.
    assert lane.status(handle.worker_id).status == WorkerStatus.DONE
    assert lane.status(handle.worker_id).cancel_requested is False
    assert lane.result(handle.worker_id).result == "done"


def test_cancel_during_uncooperative_runner_still_completes_as_done():
    entered = threading.Event()
    release = threading.Event()
    # This runner ignores the token and returns a result anyway.
    lane = ThreadWorkerLane(runner=_gated_runner(entered=entered, release=release, after=lambda s, t: "ignored cancel"))
    handle = lane.start(WorkerSpec(goal="uncooperative"))
    assert entered.wait(TIMEOUT)
    assert lane.cancel(handle.worker_id) is True

    release.set()
    assert lane.wait(handle.worker_id, timeout=TIMEOUT)
    res = lane.result(handle.worker_id)
    # The work completed, so it is DONE -- but the cancellation request is not lost.
    assert res.status == WorkerStatus.DONE
    assert res.result == "ignored cancel"
    assert lane.status(handle.worker_id).cancel_requested is True


# --------------------------------------------------------------------------
# ThreadWorkerLane -- follow-ups
# --------------------------------------------------------------------------
def test_followups_preserve_order_and_raw_is_neither_serialized_nor_touched():
    entered = threading.Event()
    release = threading.Event()
    lane = ThreadWorkerLane(runner=_gated_runner(entered=entered, release=release))
    handle = lane.start(WorkerSpec(goal="receives followups"))
    assert entered.wait(TIMEOUT)

    a = PendingTurnItem(text="first")
    b = PendingTurnItem(text="second", raw=UncopyableRaw())  # raw must survive untouched
    c = from_legacy_cli_payload(("caption", ["/tmp/a.png"]))  # a media item

    assert lane.append_followup(handle.worker_id, a) == FOLLOWUP_DEFERRED
    assert lane.append_followup(handle.worker_id, b) == FOLLOWUP_DEFERRED
    assert lane.append_followup(handle.worker_id, c) == FOLLOWUP_DEFERRED

    stored = lane.followups(handle.worker_id)
    assert [it.text for it in stored] == ["first", "second", "caption"]
    assert stored[0] is a and stored[1] is b  # stored as-is, not copied
    assert isinstance(stored[1].raw, UncopyableRaw)  # raw untouched, still present
    assert stored[2].kind == KIND_MEDIA and stored[2].media_refs == ["/tmp/a.png"]

    # Non-PendingTurnItem input is rejected with a clear error.
    with pytest.raises(TypeError, match="expects a PendingTurnItem"):
        lane.append_followup(handle.worker_id, "raw string follow-up")

    # The JSON snapshot includes follow-ups via PendingTurnItem.to_dict, which
    # drops `raw` without touching it.
    snap = lane.snapshot(handle.worker_id)
    fu_dicts = snap["followups"]
    assert [d["text"] for d in fu_dicts] == ["first", "second", "caption"]
    assert all("raw" not in d for d in fu_dicts)
    json.dumps(snap)  # JSON-safe end to end

    release.set()
    assert lane.wait(handle.worker_id, timeout=TIMEOUT)
    # Appending to a finished worker is rejected (not stored).
    assert lane.append_followup(handle.worker_id, PendingTurnItem(text="too late")) == FOLLOWUP_REJECTED
    assert [it.text for it in lane.followups(handle.worker_id)] == ["first", "second", "caption"]


# --------------------------------------------------------------------------
# Unknown / duplicate worker ids and lanes
# --------------------------------------------------------------------------
def test_unknown_worker_id_raises_clear_keyerror_everywhere():
    lane = ThreadWorkerLane(runner=_instant_runner())
    for call in (
        lambda: lane.status("worker-nope"),
        lambda: lane.result("worker-nope"),
        lambda: lane.cancel("worker-nope"),
        lambda: lane.append_followup("worker-nope", PendingTurnItem(text="x")),
        lambda: lane.wait("worker-nope", timeout=0),
        lambda: lane.followups("worker-nope"),
        lambda: lane.snapshot("worker-nope"),
    ):
        with pytest.raises(KeyError, match="unknown worker id"):
            call()


def test_worker_id_collision_is_rejected(monkeypatch):
    monkeypatch.setattr(wl, "_new_worker_id", lambda: "worker-fixed")
    lane = ThreadWorkerLane(runner=_instant_runner())
    first = lane.start(WorkerSpec(goal="one"))
    assert first.worker_id == "worker-fixed"
    with pytest.raises(RuntimeError, match="worker id collision"):
        lane.start(WorkerSpec(goal="two"))


def test_lane_construction_rejects_bad_args():
    with pytest.raises(TypeError, match="runner must be callable"):
        ThreadWorkerLane(runner="not callable")
    with pytest.raises(TypeError, match="lane name must be a non-empty string"):
        ThreadWorkerLane(runner=_instant_runner(), name="")
    with pytest.raises(TypeError, match="spec must be a WorkerSpec"):
        ThreadWorkerLane(runner=_instant_runner()).start({"goal": "not a spec"})


# --------------------------------------------------------------------------
# WorkerLaneRegistry
# --------------------------------------------------------------------------
def test_registry_routes_status_result_cancel_followup_to_the_right_lane():
    entered_a = threading.Event()
    release_a = threading.Event()
    lane_a = ThreadWorkerLane(
        runner=_gated_runner(
            entered=entered_a,
            release=release_a,
            after=lambda spec, token: token.raise_if_cancelled() or "unreached",
        ),
        name="thread",
    )
    lane_b = ThreadWorkerLane(runner=_instant_runner("from b"), name="thread-b")

    reg = WorkerLaneRegistry()
    reg.register(lane_a)
    reg.register(lane_b)
    assert set(reg.lane_names()) == {"thread", "thread-b"}
    assert reg.get_lane("thread") is lane_a

    # spec.lane drives the default target...
    h_a = reg.start(WorkerSpec(goal="on a", lane="thread"))
    # ...and an explicit lane_name override wins.
    h_b = reg.start(WorkerSpec(goal="on b", lane="thread"), lane_name="thread-b")

    assert entered_a.wait(TIMEOUT)
    assert reg.status(h_a.worker_id).status == WorkerStatus.RUNNING
    assert reg.status(h_a.worker_id).lane == "thread"
    assert reg.append_followup(h_a.worker_id, PendingTurnItem(text="steer a")) == FOLLOWUP_DEFERRED
    assert lane_a.followups(h_a.worker_id)[0].text == "steer a"

    assert reg.cancel(h_a.worker_id) is True
    release_a.set()
    assert reg.wait(h_a.worker_id, timeout=TIMEOUT)
    assert reg.result(h_a.worker_id).status == WorkerStatus.CANCELLED

    assert reg.wait(h_b.worker_id, timeout=TIMEOUT)
    assert reg.result(h_b.worker_id).status == WorkerStatus.DONE
    assert reg.result(h_b.worker_id).result == "from b"
    assert reg.status(h_b.worker_id).lane == "thread-b"


def test_registry_rejects_duplicate_lane_and_unknown_lane_or_worker():
    reg = WorkerLaneRegistry()
    reg.register(ThreadWorkerLane(runner=_instant_runner(), name="thread"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register(ThreadWorkerLane(runner=_instant_runner(), name="thread"))
    with pytest.raises(TypeError, match="non-empty string 'name'"):
        reg.register(object())

    with pytest.raises(KeyError, match="unknown worker lane"):
        reg.start(WorkerSpec(goal="x", lane="ghost"))
    with pytest.raises(KeyError, match="unknown worker lane"):
        reg.start(WorkerSpec(goal="x"), lane_name="ghost")

    for call in (
        lambda: reg.status("worker-nope"),
        lambda: reg.result("worker-nope"),
        lambda: reg.cancel("worker-nope"),
        lambda: reg.append_followup("worker-nope", PendingTurnItem(text="x")),
        lambda: reg.wait("worker-nope", timeout=0),
    ):
        with pytest.raises(KeyError, match="unknown worker id"):
            call()

    with pytest.raises(TypeError, match="spec must be a WorkerSpec"):
        reg.start({"goal": "nope"})


def test_registry_rejects_cross_lane_worker_id_collision(monkeypatch):
    monkeypatch.setattr(wl, "_new_worker_id", lambda: "worker-fixed")
    lane_a = ThreadWorkerLane(runner=_instant_runner("a"), name="thread-a")
    lane_b = ThreadWorkerLane(runner=_instant_runner("b"), name="thread-b")
    reg = WorkerLaneRegistry()
    reg.register(lane_a)
    reg.register(lane_b)

    first = reg.start(WorkerSpec(goal="a", lane="thread-a"))
    assert first.worker_id == "worker-fixed"
    with pytest.raises(RuntimeError, match="worker id collision across lanes"):
        reg.start(WorkerSpec(goal="b", lane="thread-b"))

    assert reg.wait(first.worker_id, timeout=TIMEOUT)
    assert reg.result(first.worker_id).result == "a"
    assert reg.status(first.worker_id).lane == "thread-a"


# --------------------------------------------------------------------------
# Optional TaskRegistry linkage
# --------------------------------------------------------------------------
def test_link_worker_to_task_records_worker_metadata_on_the_focused_task():
    reg = TaskRegistry()
    task = reg.create_task("heavy implementation", session_key="s1")
    lane = ThreadWorkerLane(runner=_instant_runner("done"))
    handle = lane.start(WorkerSpec(goal="heavy implementation", task_id=task.task_id))

    returned = link_worker_to_task(reg, task.task_id, handle)
    assert returned is task
    assert task.active_worker_id == handle.worker_id
    assert task.worker_kind == LANE_THREAD  # defaults to the lane name

    # An explicit worker_kind overrides the default.
    link_worker_to_task(reg, task.task_id, handle, worker_kind="ralph")
    assert task.worker_kind == "ralph"

    # No classifier / routing was started: the registry still has exactly one task,
    # and the task's status is whatever we set it to (linkage does not touch it).
    assert len(reg) == 1
    assert task.status == "proposed"

    assert lane.wait(handle.worker_id, timeout=TIMEOUT)
    assert lane.result(handle.worker_id).status == WorkerStatus.DONE


# --------------------------------------------------------------------------
# JSON snapshots vs non-JSON-safe metadata
# --------------------------------------------------------------------------
def test_snapshots_reject_non_json_safe_metadata():
    # WorkerSpec.to_dict refuses non-JSON-safe metadata loudly...
    with pytest.raises(TypeError, match="metadata must be JSON-serializable"):
        WorkerSpec(goal="g", metadata={"bad": object()}).to_dict()
    with pytest.raises(TypeError, match="metadata must be JSON-serializable"):
        WorkerSpec(goal="g", metadata={"nan": float("nan")}).to_dict()
    with pytest.raises(TypeError, match="metadata must be JSON-serializable"):
        WorkerSpec(goal="g", metadata={"inf": float("inf")}).to_dict()

    # ...and a lane snapshot that would have to embed such a spec raises too,
    # rather than silently dropping or faking it.
    lane = ThreadWorkerLane(runner=_instant_runner())
    bad = lane.start(WorkerSpec(goal="bad metadata", metadata={"obj": object()}))
    assert lane.wait(bad.worker_id, timeout=TIMEOUT)
    with pytest.raises(TypeError, match="metadata must be JSON-serializable"):
        lane.snapshot(bad.worker_id)
    with pytest.raises(TypeError, match="metadata must be JSON-serializable"):
        lane.snapshot()

    # A worker with JSON-safe metadata snapshots cleanly.
    ok = lane.start(WorkerSpec(goal="fine", task_id="task-9", metadata={"depth": 3}))
    assert lane.wait(ok.worker_id, timeout=TIMEOUT)
    snap = lane.snapshot(ok.worker_id)
    json.dumps(snap)
    assert snap["worker_id"] == ok.worker_id
    assert snap["status"] == WorkerStatus.DONE
    assert snap["spec"]["metadata"] == {"depth": 3}
    assert snap["spec"]["task_id"] == "task-9"
    assert snap["followups"] == []
