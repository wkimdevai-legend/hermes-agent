"""Tests for the minimal frontdesk control-loop runtime."""

import threading

from agent.control_plane import Intent, Recommendation
from agent.orchestration_runtime import OrchestrationRuntime
from agent.task_registry import STATUS_CANCELLED, STATUS_RUNNING
from agent.worker_lanes import CancelToken, ThreadWorkerLane, WorkerSpec, WorkerStatus


def test_frontdesk_worker_decision_creates_task_and_starts_registered_worker_lane():
    runtime = OrchestrationRuntime.create()
    entered = threading.Event()

    def runner(spec: WorkerSpec, token: CancelToken):  # noqa: ARG001
        entered.set()
        return f"done:{spec.goal}"

    runtime.worker_registry.register(ThreadWorkerLane(runner=runner, name="thread"))

    result = runtime.handle_frontdesk_input(
        "draft a report.md with the audit",
        frontdesk_mode_active=True,
        session_key="s1",
        source_surface="gateway",
    )

    assert result.decision.intent is Intent.NEW_TASK_WORKER
    assert result.decision.recommendation is Recommendation.WORKER_LANE
    assert result.action == "worker_started"
    assert result.task_id is not None
    assert result.worker_id is not None
    task = runtime.task_registry.get_task(result.task_id)
    assert task is not None
    assert task.status == STATUS_RUNNING
    assert task.active_worker_id == result.worker_id
    assert task.worker_kind == "thread"
    assert entered.wait(2.0)
    assert runtime.worker_registry.wait(result.worker_id, timeout=2.0)
    worker_result = runtime.worker_registry.result(result.worker_id)
    assert worker_result is not None
    assert worker_result.status == WorkerStatus.DONE


def test_frontdesk_status_returns_local_overview_without_starting_worker():
    runtime = OrchestrationRuntime.create()
    runtime.task_registry.create_task("existing", session_key="s1", status=STATUS_RUNNING)

    result = runtime.handle_frontdesk_input(
        "지금 뭐 하고 있어?",
        frontdesk_mode_active=True,
        session_key="s1",
    )

    assert result.decision.intent is Intent.STATUS
    assert result.action == "status"
    assert "Tasks:" in result.message or "Active tasks:" in result.message
    assert "existing" in result.message
    assert result.task_id is None
    assert result.worker_id is None


def test_frontdesk_stop_cancels_active_workers_and_tasks_without_replay():
    runtime = OrchestrationRuntime.create()
    release = threading.Event()

    def runner(spec: WorkerSpec, token: CancelToken):  # noqa: ARG001
        release.wait(2.0)
        token.raise_if_cancelled()
        return "should not matter"

    runtime.worker_registry.register(ThreadWorkerLane(runner=runner, name="thread"))
    started = runtime.handle_frontdesk_input(
        "draft a report.md with the audit",
        frontdesk_mode_active=True,
        session_key="s1",
    )
    assert started.worker_id is not None
    assert started.task_id is not None

    stopped = runtime.handle_frontdesk_input("그만", frontdesk_mode_active=True, session_key="s1")

    assert stopped.decision.intent is Intent.STOP
    assert stopped.action == "stopped"
    assert stopped.cancelled_tasks == 1
    assert stopped.cancelled_workers == 1
    task = runtime.task_registry.get_task(started.task_id)
    assert task is not None
    assert task.status == STATUS_CANCELLED
    release.set()
    assert runtime.worker_registry.wait(started.worker_id, timeout=2.0)
    assert runtime.worker_registry.status(started.worker_id).cancel_requested is True


def test_frontdesk_steer_calls_callback_when_main_is_in_flight():
    runtime = OrchestrationRuntime.create()
    steers = []

    result = runtime.handle_frontdesk_input(
        "also update the config file",
        frontdesk_mode_active=True,
        main_in_flight=True,
        steer_callback=steers.append,
    )

    assert result.decision.intent is Intent.STEER
    assert result.action == "steered"
    assert steers == ["also update the config file"]
    assert result.message.startswith("control: steered")


def test_frontdesk_steer_without_running_main_falls_back_to_main():
    runtime = OrchestrationRuntime.create()

    result = runtime.handle_frontdesk_input(
        "also update the config file",
        frontdesk_mode_active=True,
        main_in_flight=False,
    )

    assert result.decision.intent is Intent.STEER
    assert result.action == "main"
    assert "no active main turn" in result.message
