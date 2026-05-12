"""Worker-lane / detached-execution substrate for Hermes (orchestrator Phase 4).

Hermes is moving toward a concierge / front-desk / butler orchestrator: the main
process stays accountable for user intent, prioritisation, review and synthesis,
but long implementation / research work should eventually run in *worker lanes*
so the foreground orchestrator can acknowledge dispatch and remain available for
the next Telegram/CLI/TUI turn.  Phase 3 gave focused tasks an explicit identity
and state (:mod:`agent.task_registry`).  This module is the next layer down: a
small, explicit *worker-lane* abstraction plus one local, fully-testable lane
implementation that can start, track, cancel and retrieve results for detached
work without blocking the caller until completion.

What this module is -- and isn't:

* It is a *library substrate*.  :class:`WorkerLane` is a structural protocol;
  :class:`ThreadWorkerLane` is one purpose-fit lane that runs a caller-supplied
  ``runner`` callable in a daemon thread; :class:`WorkerLaneRegistry` is a tiny
  name -> lane index with a worker-id -> lane back-pointer so callers can address
  a worker by id without tracking which lane owns it.  :func:`link_worker_to_task`
  is a one-line convenience that records ``active_worker_id`` / ``worker_kind`` on
  a supplied :class:`~agent.task_registry.TaskRegistry`.
* It is **not** the Ralph / focused-agent runtime, **not** a follow-up
  classifier, **not** automatic Telegram/gateway routing into workers, **not**
  the user-facing ``/tasks`` / ``/agents`` / ``/stop`` commands, and **not** the
  ``delegate_task(background=True)`` tool API.  It deliberately ships no Claude
  Code process lane, no Kanban lane, and no SQLite / multi-process durable worker
  database.  Those are later phases; this module is only the foundation they will
  connect to.

Scope discipline (mirrors the Phase 2/3 leaf modules):

* This module is a leaf: it imports only the standard library and the Phase 2
  :mod:`agent.pending_turn_queue` (for :class:`~agent.pending_turn_queue.PendingTurnItem`,
  used by :meth:`WorkerLane.append_followup`).  The optional task-registry
  linkage helper is duck-typed -- it calls ``registry.assign_worker(...)`` and
  never imports :mod:`agent.task_registry` at runtime -- so this module stays a
  leaf and works even where the registry is not present.
* Everything produced by a ``to_dict`` / ``snapshot`` method is plain JSON-safe
  data.  Follow-ups are serialised via :meth:`PendingTurnItem.to_dict`, which
  drops the one local-process passthrough (``PendingTurnItem.raw``) without
  touching it.  ``WorkerSpec.metadata`` is documented to be JSON-safe and the
  snapshot path *enforces* it (a non-JSON-safe value raises ``TypeError`` rather
  than being silently serialised) so a snapshot never lies about JSON safety.
* Cancellation is *cooperative*.  :meth:`ThreadWorkerLane.cancel` sets a
  :class:`CancelToken` (and, if the worker has not yet entered its runner, the
  worker transitions straight to ``cancelled``); it never forcibly stops a
  running thread.  A runner that observes the token and raises
  :class:`WorkerCancelled` ends as ``cancelled``; one that ignores the token and
  returns normally ends as ``done`` -- ``cancel_requested`` independently records
  that a cancellation was asked for.  Full kill semantics for arbitrary external
  processes are explicitly out of scope.
* In-process thread-safety is handled here (each lane and the registry hold their
  own lock); the optional :class:`~agent.task_registry.TaskRegistry` linkage is
  *not* thread-safe and that is the caller's responsibility, exactly as the
  registry's own docstring says.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, Callable, Protocol

from agent.pending_turn_queue import PendingTurnItem

if TYPE_CHECKING:  # pragma: no cover - typing only; never imported at runtime
    from agent.task_registry import FocusedTask, TaskRegistry

__all__ = [
    "WorkerStatus",
    "WORKER_STATUSES",
    "ACTIVE_WORKER_STATUSES",
    "TERMINAL_WORKER_STATUSES",
    "LANE_THREAD",
    "FOLLOWUP_ACCEPTED",
    "FOLLOWUP_DEFERRED",
    "FOLLOWUP_REJECTED",
    "WorkerSpec",
    "WorkerHandle",
    "WorkerResult",
    "WorkerLane",
    "WorkerCancelled",
    "CancelToken",
    "ThreadWorkerLane",
    "WorkerLaneRegistry",
    "link_worker_to_task",
]


# --------------------------------------------------------------------------
# Vocabulary.
#
# The worker status set is *closed* (like ``task_registry``'s task statuses, and
# unlike ``pending_turn_queue``'s open ``kind`` / ``boundary`` strings): every
# transition is driven internally by a lane, so a status outside this set is a
# bug.  ``WorkerStatus`` is a plain namespace of the five strings, not an enum.
# --------------------------------------------------------------------------
class WorkerStatus:
    """The five worker lifecycle states (a namespace of strings, not an enum)."""

    QUEUED = "queued"        # accepted by a lane, runner not yet entered
    RUNNING = "running"      # runner is executing
    DONE = "done"            # runner returned normally
    ERROR = "error"          # runner raised a non-cancel exception
    CANCELLED = "cancelled"  # cancelled before/while running (cooperatively)


TERMINAL_WORKER_STATUSES = frozenset(
    {WorkerStatus.DONE, WorkerStatus.ERROR, WorkerStatus.CANCELLED}
)
ACTIVE_WORKER_STATUSES = frozenset({WorkerStatus.QUEUED, WorkerStatus.RUNNING})
WORKER_STATUSES = ACTIVE_WORKER_STATUSES | TERMINAL_WORKER_STATUSES

# Default lane name.  Free-form: later phases can name new lanes ("claude_code",
# "terminal", "ralph", ...) without touching this module -- this is the one this
# module actually implements.
LANE_THREAD = "thread"

# Disposition strings returned by :meth:`WorkerLane.append_followup`.  This lane
# only *stores* follow-ups (it does not yet inject them into a running worker), so
# a follow-up accepted onto a live worker is reported as ``deferred``; a follow-up
# offered to an already-finished worker is ``rejected``.  A future steerable lane
# may return ``accepted`` once it can actually splice a follow-up into a run.
FOLLOWUP_ACCEPTED = "accepted"
FOLLOWUP_DEFERRED = "deferred"
FOLLOWUP_REJECTED = "rejected"


def _new_worker_id() -> str:
    return f"worker-{uuid.uuid4().hex}"


def _json_safe_copy(value: Any, *, label: str) -> Any:
    """Return a detached JSON-safe copy of *value* or raise ``TypeError``.

    Used only for ``WorkerSpec.metadata`` on the snapshot path -- it never touches
    ``PendingTurnItem.raw``.  ``allow_nan=False`` so ``float('nan')`` / ``inf`` are
    rejected too (matching :mod:`agent.task_registry`).
    """
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} must be JSON-serializable") from exc


def _coerce_result(value: Any) -> str | None:
    """Coerce a runner return value to the ``str | None`` shape of ``WorkerResult.result``."""
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


# --------------------------------------------------------------------------
# Cooperative cancellation
# --------------------------------------------------------------------------
class WorkerCancelled(Exception):
    """Raised by a runner to signal it observed a cancellation request and stopped.

    A runner that raises this ends with status ``cancelled``.  A runner that does
    not cooperate (ignores the :class:`CancelToken` and returns/raises something
    else) is not interrupted -- this substrate's cancellation is cooperative.
    """


class CancelToken:
    """A one-shot cancellation flag handed to a worker runner.

    A runner should poll :attr:`cancelled` (or call :meth:`raise_if_cancelled`) at
    safe checkpoints, or block on :meth:`wait`.  Setting the token does not
    interrupt the running thread; it only requests that the runner stop on its own.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def request(self) -> None:
        """Request cancellation (idempotent)."""
        self._event.set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`WorkerCancelled` if cancellation has been requested."""
        if self._event.is_set():
            raise WorkerCancelled("cancellation requested")

    def wait(self, timeout: float | None = None) -> bool:
        """Block up to *timeout* seconds; return ``True`` if cancelled meanwhile."""
        return self._event.wait(timeout)


# --------------------------------------------------------------------------
# Public dataclasses: WorkerSpec / WorkerHandle / WorkerResult
# --------------------------------------------------------------------------
@dataclass
class WorkerSpec:
    """What to run in a worker lane.

    *goal* is the human description of the work; *context* is optional extra prose
    (the equivalent of a delegate-task "context" blob); *task_id* optionally links
    to a :class:`~agent.task_registry.FocusedTask`; *lane* names the desired lane
    (a registry uses it as the default target); *metadata* is free-form,
    *documented to be JSON-safe* descriptive data (it must be a ``dict`` -- the
    runner is supplied to the lane, never smuggled through here).
    """

    goal: str
    context: str | None = None
    task_id: str | None = None
    lane: str = LANE_THREAD
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.goal, str):
            raise TypeError("WorkerSpec.goal must be a string")
        if not isinstance(self.lane, str) or not self.lane.strip():
            raise TypeError("WorkerSpec.lane must be a non-empty string")
        if self.context is not None and not isinstance(self.context, str):
            raise TypeError("WorkerSpec.context must be a string or None")
        if self.task_id is not None and not isinstance(self.task_id, str):
            raise TypeError("WorkerSpec.task_id must be a string or None")
        if not isinstance(self.metadata, dict):
            raise TypeError("WorkerSpec.metadata must be a dict")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict; raises ``TypeError`` on non-JSON-safe metadata."""
        return {
            "goal": self.goal,
            "context": self.context,
            "task_id": self.task_id,
            "lane": self.lane,
            "metadata": _json_safe_copy(self.metadata, label="WorkerSpec.metadata"),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "WorkerSpec":
        """Rebuild from :meth:`to_dict` output (unknown keys ignored)."""
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**kw)


@dataclass
class WorkerHandle:
    """A point-in-time view of a worker's identity and lifecycle state.

    Returned by :meth:`WorkerLane.start` / :meth:`WorkerLane.status`.  ``lane`` is
    the *name of the lane actually running it* (which a registry may have chosen
    over ``WorkerSpec.lane``).  ``cancel_requested`` makes a cancellation request
    observable without a second call -- the suggested shape lists the core fields;
    this one extra keeps "the cancellation request is represented" honest.
    """

    worker_id: str
    task_id: str | None
    lane: str
    status: str
    created_at: float
    updated_at: float
    cancel_requested: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_WORKER_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "task_id": self.task_id,
            "lane": self.lane,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "cancel_requested": self.cancel_requested,
        }


@dataclass
class WorkerResult:
    """The outcome of a finished worker.

    Obtained from :meth:`WorkerLane.result`, which returns ``None`` until the
    worker is terminal.  ``result`` is set only on ``done``; ``error`` is set only
    on ``error`` (``cancelled`` carries neither -- ``cancel_requested`` on the
    handle is where a cancellation shows up).
    """

    worker_id: str
    status: str
    result: str | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# --------------------------------------------------------------------------
# WorkerLane protocol
# --------------------------------------------------------------------------
class WorkerLane(Protocol):
    """Structural interface every worker lane implements.

    A lane owns a set of workers it identifies by ``worker_id`` strings it mints.
    :meth:`start` must return promptly -- it must not block until the worker
    finishes.  :meth:`status` returns the current :class:`WorkerHandle`;
    :meth:`result` returns a :class:`WorkerResult` once (and only once) the worker
    is terminal, else ``None``.  :meth:`append_followup` records a
    :class:`~agent.pending_turn_queue.PendingTurnItem` against the worker and
    returns a disposition string (one of ``FOLLOWUP_ACCEPTED`` /
    ``FOLLOWUP_DEFERRED`` / ``FOLLOWUP_REJECTED``).  :meth:`cancel` requests
    cancellation and returns ``True`` if the worker was still active, ``False`` if
    it had already finished (a safe no-op).  Unknown ``worker_id`` raises
    ``KeyError`` everywhere it is accepted.
    """

    name: str

    def start(self, spec: "WorkerSpec") -> "WorkerHandle": ...

    def status(self, worker_id: str) -> "WorkerHandle": ...

    def append_followup(self, worker_id: str, item: PendingTurnItem) -> str: ...

    def cancel(self, worker_id: str) -> bool: ...

    def result(self, worker_id: str) -> "WorkerResult | None": ...


# --------------------------------------------------------------------------
# Internal per-worker state
# --------------------------------------------------------------------------
@dataclass
class _WorkerEntry:
    """A lane's private record for one worker.  Never serialised as-is; the
    thread / cancel token / done-event are local-only, and :meth:`snapshot` builds
    a JSON-safe view."""

    worker_id: str
    spec: WorkerSpec
    lane_name: str
    status: str
    created_at: float
    updated_at: float
    started_at: float | None = None
    finished_at: float | None = None
    result: str | None = None
    error: str | None = None
    cancel_requested: bool = False
    cancel_token: CancelToken = field(default_factory=CancelToken)
    followups: list[PendingTurnItem] = field(default_factory=list)
    thread: threading.Thread | None = None
    done_event: threading.Event = field(default_factory=threading.Event)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_WORKER_STATUSES

    def touch(self, *, now: float | None = None) -> None:
        self.updated_at = time.time() if now is None else float(now)

    def handle(self) -> WorkerHandle:
        return WorkerHandle(
            worker_id=self.worker_id,
            task_id=self.spec.task_id,
            lane=self.lane_name,
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
            cancel_requested=self.cancel_requested,
        )

    def result_snapshot(self) -> WorkerResult:
        return WorkerResult(
            worker_id=self.worker_id,
            status=self.status,
            result=self.result,
            error=self.error,
            started_at=self.started_at,
            finished_at=self.finished_at,
        )

    def snapshot_dict(self) -> dict[str, Any]:
        """JSON-safe view: the handle fields plus result/error/timestamps, the
        appended follow-ups (via :meth:`PendingTurnItem.to_dict`, which drops
        ``raw``), and the originating spec (via :meth:`WorkerSpec.to_dict`, which
        raises ``TypeError`` on non-JSON-safe metadata -- a snapshot does not
        pretend a non-serialisable spec is fine)."""
        data = self.handle().to_dict()
        data.update(
            {
                "result": self.result,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "followups": [it.to_dict() for it in self.followups],
                "spec": self.spec.to_dict(),
            }
        )
        return data


# --------------------------------------------------------------------------
# ThreadWorkerLane
# --------------------------------------------------------------------------
class ThreadWorkerLane:
    """A purpose-fit, in-process worker lane that runs each worker in a daemon thread.

    Construction takes a *runner* callable, ``runner(spec: WorkerSpec, token:
    CancelToken) -> str | None``.  The runner does the actual work; this lane only
    tracks lifecycle, captures the return value (coerced to ``str | None``) or the
    exception, holds appended follow-ups, and exposes a cancellation request the
    runner may observe via *token*.

    Lifecycle: a worker is created ``queued``; the thread flips it to ``running``
    just before invoking the runner; then ``done`` (runner returned), ``error``
    (runner raised anything other than :class:`WorkerCancelled`, with
    ``"<ExcType>: <msg>"`` recorded in ``error``), or ``cancelled`` (the runner
    raised :class:`WorkerCancelled`, or :meth:`cancel` was called before the
    runner was entered).  If :meth:`cancel` is called while the runner is running
    and the runner returns normally anyway, the worker ends ``done`` -- the work
    completed -- but ``cancel_requested`` stays ``True`` so the request is not lost.

    All shared state is guarded by a single lock; the runner is invoked *without*
    the lock held, so :meth:`status` / :meth:`cancel` / :meth:`append_followup`
    stay responsive while a worker runs.
    """

    def __init__(
        self,
        *,
        runner: Callable[[WorkerSpec, CancelToken], Any],
        name: str = LANE_THREAD,
    ) -> None:
        if not callable(runner):
            raise TypeError("runner must be callable")
        if not isinstance(name, str) or not name:
            raise TypeError("lane name must be a non-empty string")
        self._runner = runner
        self.name = name
        self._lock = threading.Lock()
        self._workers: dict[str, _WorkerEntry] = {}

    # -- internals --------------------------------------------------------
    def _require(self, worker_id: str) -> _WorkerEntry:
        entry = self._workers.get(worker_id)
        if entry is None:
            raise KeyError(f"unknown worker id: {worker_id!r}")
        return entry

    def _finalize_locked(
        self,
        entry: _WorkerEntry,
        status: str,
        *,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        entry.status = status
        entry.result = result
        entry.error = error
        entry.finished_at = time.time()
        entry.touch()
        entry.done_event.set()

    def _run(self, worker_id: str) -> None:
        with self._lock:
            entry = self._workers[worker_id]
            if entry.cancel_token.cancelled:
                # Cancelled in the narrow window between start() and the thread
                # acquiring this lock: never enter the runner.
                self._finalize_locked(entry, WorkerStatus.CANCELLED)
                return
            entry.status = WorkerStatus.RUNNING
            entry.started_at = time.time()
            entry.touch()
            spec = entry.spec
            token = entry.cancel_token
        runner = self._runner
        try:
            out = runner(spec, token)
        except WorkerCancelled:
            with self._lock:
                self._finalize_locked(self._workers[worker_id], WorkerStatus.CANCELLED)
            return
        except BaseException as exc:  # noqa: BLE001 - capture *any* runner failure
            detail = f"{type(exc).__name__}: {exc}".strip().rstrip(":").strip()
            with self._lock:
                self._finalize_locked(
                    self._workers[worker_id], WorkerStatus.ERROR, error=detail or type(exc).__name__
                )
            return
        with self._lock:
            # Runner returned normally.  Even if a cancellation was requested
            # mid-run, the work completed -- record it as done; cancel_requested
            # already preserves that a cancellation was asked for.
            self._finalize_locked(
                self._workers[worker_id], WorkerStatus.DONE, result=_coerce_result(out)
            )

    # -- WorkerLane protocol ---------------------------------------------
    def start(self, spec: WorkerSpec) -> WorkerHandle:
        """Register a worker and start its thread; return its (``queued``) handle."""
        if not isinstance(spec, WorkerSpec):
            raise TypeError("spec must be a WorkerSpec")
        now = time.time()
        worker_id = _new_worker_id()
        with self._lock:
            if worker_id in self._workers:  # astronomically unlikely; be explicit
                raise RuntimeError(f"worker id collision: {worker_id!r}")
            entry = _WorkerEntry(
                worker_id=worker_id,
                spec=spec,
                lane_name=self.name,
                status=WorkerStatus.QUEUED,
                created_at=now,
                updated_at=now,
            )
            self._workers[worker_id] = entry
            thread = threading.Thread(
                target=self._run,
                args=(worker_id,),
                name=f"worker-lane-{self.name}-{worker_id[7:15]}",
                daemon=True,
            )
            entry.thread = thread
            # Start the thread while still holding the lock: the new thread blocks
            # on this lock inside _run, so the handle we return below is a clean
            # "queued" snapshot rather than a race against the transition to
            # "running".
            thread.start()
            return entry.handle()

    def status(self, worker_id: str) -> WorkerHandle:
        with self._lock:
            return self._require(worker_id).handle()

    def result(self, worker_id: str) -> WorkerResult | None:
        with self._lock:
            entry = self._require(worker_id)
            return entry.result_snapshot() if entry.is_terminal else None

    def append_followup(self, worker_id: str, item: PendingTurnItem) -> str:
        """Record a follow-up against the worker; see ``FOLLOWUP_*`` for the result.

        Stores the :class:`~agent.pending_turn_queue.PendingTurnItem` in arrival
        order without injecting it into a running worker (that is a later
        steerable-lane concern) -- so a live worker yields ``FOLLOWUP_DEFERRED`` and
        a finished one yields ``FOLLOWUP_REJECTED``.  Non-``PendingTurnItem`` input
        raises ``TypeError`` (lift legacy payloads with
        :func:`agent.pending_turn_queue.from_legacy_cli_payload` first).
        """
        if not isinstance(item, PendingTurnItem):
            raise TypeError(
                "append_followup expects a PendingTurnItem; lift legacy payloads "
                "with agent.pending_turn_queue.from_legacy_cli_payload first"
            )
        with self._lock:
            entry = self._require(worker_id)
            if entry.is_terminal:
                return FOLLOWUP_REJECTED
            entry.followups.append(item)
            entry.touch()
            return FOLLOWUP_DEFERRED

    def cancel(self, worker_id: str) -> bool:
        """Request cancellation.  ``True`` if the worker was still active; ``False``
        (a safe no-op) if it had already finished.  Cooperative: the runner must
        observe the token to actually stop early."""
        with self._lock:
            entry = self._require(worker_id)
            if entry.is_terminal:
                return False
            entry.cancel_requested = True
            entry.cancel_token.request()
            entry.touch()
            return True

    # -- extras (not part of the WorkerLane protocol) --------------------
    def wait(self, worker_id: str, timeout: float | None = None) -> bool:
        """Block until the worker is terminal; ``True`` if it is (within *timeout*),
        ``False`` on timeout.  Unknown ``worker_id`` raises ``KeyError``."""
        with self._lock:
            entry = self._require(worker_id)
            if entry.is_terminal:
                return True
            done = entry.done_event
        return done.wait(timeout)

    def followups(self, worker_id: str) -> list[PendingTurnItem]:
        """A shallow copy of the worker's appended follow-ups, in arrival order."""
        with self._lock:
            return list(self._require(worker_id).followups)

    def worker_ids(self) -> list[str]:
        """The ids of all workers this lane has started, in creation order."""
        with self._lock:
            return list(self._workers.keys())

    def snapshot(self, worker_id: str | None = None) -> dict[str, Any]:
        """A JSON-safe snapshot.  With *worker_id*: just that worker's
        :meth:`_WorkerEntry.snapshot_dict`.  Without: ``{"lane": name, "workers":
        [<per-worker dict>, ...]}``.  Each per-worker dict embeds the spec, so a
        worker carrying non-JSON-safe metadata makes the snapshot raise
        ``TypeError`` -- a snapshot will not silently drop or fake JSON safety."""
        with self._lock:
            if worker_id is not None:
                return self._require(worker_id).snapshot_dict()
            return {
                "lane": self.name,
                "workers": [e.snapshot_dict() for e in self._workers.values()],
            }


# --------------------------------------------------------------------------
# WorkerLaneRegistry
# --------------------------------------------------------------------------
class WorkerLaneRegistry:
    """A tiny registry of named :class:`WorkerLane` instances plus a worker-id ->
    lane index, so callers can :meth:`start` on a lane by name and then
    :meth:`status` / :meth:`result` / :meth:`append_followup` / :meth:`cancel` a
    worker by id without tracking which lane owns it.

    It starts nothing on its own and reacts to no completion: routing of user
    follow-ups into workers, gateway notification on completion, and the
    user-facing ``/tasks`` / ``/agents`` / ``/stop`` surface are all later phases.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lanes: dict[str, WorkerLane] = {}
        self._worker_lane: dict[str, str] = {}

    # -- registration -----------------------------------------------------
    def register(self, lane: WorkerLane) -> None:
        """Register *lane* under ``lane.name``; a duplicate name raises ``ValueError``."""
        name = getattr(lane, "name", None)
        if not isinstance(name, str) or not name:
            raise TypeError("lane must expose a non-empty string 'name'")
        with self._lock:
            if name in self._lanes:
                raise ValueError(f"worker lane already registered: {name!r}")
            self._lanes[name] = lane

    def lane_names(self) -> list[str]:
        with self._lock:
            return list(self._lanes.keys())

    def get_lane(self, name: str) -> WorkerLane | None:
        with self._lock:
            return self._lanes.get(name)

    # -- internals --------------------------------------------------------
    def _require_lane(self, name: str) -> WorkerLane:
        with self._lock:
            lane = self._lanes.get(name)
        if lane is None:
            raise KeyError(f"unknown worker lane: {name!r}")
        return lane

    def _lane_for_worker(self, worker_id: str) -> WorkerLane:
        with self._lock:
            name = self._worker_lane.get(worker_id)
        if name is None:
            raise KeyError(f"unknown worker id: {worker_id!r}")
        return self._require_lane(name)

    # -- routed operations ------------------------------------------------
    def start(self, spec: WorkerSpec, *, lane_name: str | None = None) -> WorkerHandle:
        """Start *spec* on lane *lane_name* (default: ``spec.lane``) and remember
        which lane owns the resulting worker.  Unknown lane raises ``KeyError``.

        (The packet sketch shows ``start(lane_name, spec)``; reading ``spec.lane``
        with an optional override means a caller who already set ``spec.lane`` does
        not have to repeat it.)
        """
        if not isinstance(spec, WorkerSpec):
            raise TypeError("spec must be a WorkerSpec")
        name = lane_name if lane_name is not None else spec.lane
        lane = self._require_lane(name)
        handle = lane.start(spec)
        with self._lock:
            if handle.worker_id in self._worker_lane:
                cancel = getattr(lane, "cancel", None)
                if callable(cancel):
                    cancel(handle.worker_id)
                raise RuntimeError(f"worker id collision across lanes: {handle.worker_id!r}")
            self._worker_lane[handle.worker_id] = name
        return handle

    def status(self, worker_id: str) -> WorkerHandle:
        return self._lane_for_worker(worker_id).status(worker_id)

    def result(self, worker_id: str) -> WorkerResult | None:
        return self._lane_for_worker(worker_id).result(worker_id)

    def append_followup(self, worker_id: str, item: PendingTurnItem) -> str:
        return self._lane_for_worker(worker_id).append_followup(worker_id, item)

    def cancel(self, worker_id: str) -> bool:
        return self._lane_for_worker(worker_id).cancel(worker_id)

    def wait(self, worker_id: str, timeout: float | None = None) -> bool:
        """Delegate to the owning lane's ``wait`` if it has one; otherwise raise
        ``TypeError`` (the bare :class:`WorkerLane` protocol does not require it,
        but :class:`ThreadWorkerLane` provides it)."""
        lane = self._lane_for_worker(worker_id)
        waiter = getattr(lane, "wait", None)
        if not callable(waiter):
            raise TypeError(f"lane {getattr(lane, 'name', lane)!r} does not support wait()")
        return waiter(worker_id, timeout)


# --------------------------------------------------------------------------
# Optional task-registry linkage (duck-typed; no runtime import of task_registry)
# --------------------------------------------------------------------------
def link_worker_to_task(
    registry: "TaskRegistry",
    task_id: str,
    handle: "WorkerHandle",
    *,
    worker_kind: str | None = None,
) -> "FocusedTask":
    """Record *handle* as the active worker for *task_id* on *registry*.

    A thin convenience over :meth:`TaskRegistry.assign_worker`: it links *identity*
    only.  It does **not** start a worker, classify follow-ups, route gateway
    messages, or react to the worker finishing -- those are later phases.
    ``worker_kind`` defaults to the worker's lane name (e.g. ``"thread"``).  The
    registry is not thread-safe; serialising access to it is the caller's job, as
    its own docstring says.  Returns whatever ``assign_worker`` returns (the
    :class:`~agent.task_registry.FocusedTask`).
    """
    return registry.assign_worker(
        task_id, handle.worker_id, worker_kind=worker_kind or handle.lane
    )
