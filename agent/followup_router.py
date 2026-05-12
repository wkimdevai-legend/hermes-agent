"""Conservative follow-up routing layer for Hermes (orchestrator Phase 5).

Hermes is becoming a front-desk / concierge / butler orchestrator: while a
focused task or a worker lane is active, the user may fire several short
follow-up messages -- a status query, a "stop", an "oh also...", a "no, not
that", a "actually, new task: ...", a stray slash command, a screenshot.  A
later phase will let an LLM disambiguate the genuinely ambiguous ones, but the
*first* routing layer should be deterministic, append-only by default, and
unwilling to guess: a misrouted append is harmless, but an over-confident
"cancel everything" or a destructive mutation of a task goal is not.

This module is that deterministic layer.  It sits over the Phase 2/3/4 leaf
substrates and turns one :class:`~agent.pending_turn_queue.PendingTurnItem` into
a :class:`FollowupDecision`:

    PendingTurnItem -> classify() -> FollowupDecision -> (optional) route()

* :meth:`FollowupRouter.classify` is *pure*: given the item and the list of
  active :class:`~agent.task_registry.FocusedTask` objects, it returns a
  decision (intent / confidence / target ids / action / reason / optional
  message) and mutates nothing.
* :meth:`FollowupRouter.route` runs ``classify`` against an actual
  :class:`~agent.task_registry.TaskRegistry` (filtered by session) and then
  performs the *one safe mutation* the decision calls for: attach the item as a
  follow-up, append a correction note (and still attach the item), request
  cancellation on a known worker via an optional
  :class:`~agent.worker_lanes.WorkerLaneRegistry`, create a new task, or -- for
  status queries, command boundaries and ambiguity -- do nothing but report.

Scope discipline (this module is a *policy substrate*, not the behaviour):

* It is **not** a Ralph / focused-agent runtime, **not** a model/LLM classifier,
  **not** automatic Telegram/gateway routing, **not** the user-facing ``/tasks``
  / ``/agents`` / ``/stop`` commands, and **not** the public
  ``delegate_task(background=True)`` API.  ``route`` never starts a worker,
  never delivers a worker result to chat, never force-kills anything, and never
  steers a live model context.  Those are later phases; this is the conservative
  layer they will sit on.
* It is a leaf w.r.t. Hermes: it imports only the standard library and the
  Phase 2 :mod:`agent.pending_turn_queue`.  The Phase 3
  :class:`~agent.task_registry.TaskRegistry` / Phase 4
  :class:`~agent.worker_lanes.WorkerLaneRegistry` it operates on are *passed in*
  and used purely duck-typed (``list_tasks`` / ``attach_followup`` / ``add_note``
  / ``get_task`` / ``create_task`` ; ``cancel`` / ``status``), so this module
  does not import them at runtime and never touches a ``gateway`` module.
* It never serialises, deep-copies, or otherwise touches
  :attr:`~agent.pending_turn_queue.PendingTurnItem.raw`.  When ``route`` attaches
  an item it hands the *same* object to ``TaskRegistry.attach_followup``, which
  stores it as-is -- so a local-process passthrough survives untouched.

The trigger-word matching is intentionally simple: ASCII words/phrases match on
word boundaries (case-insensitively); non-ASCII (Korean) triggers match as plain
substrings.  Korean morphology makes true word boundaries hard, so substring
matching can over-match -- which is *why* the routing rails matter: cancel only
"fires" against a single active task with a linked worker and is cooperative;
corrections are append-only; everything genuinely ambiguous is deferred.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Iterable

from agent.pending_turn_queue import (
    BOUNDARY_COMMAND,
    KIND_ATTACHMENT,
    KIND_COMMAND,
    KIND_CONTROL,
    KIND_MEDIA,
    PendingTurnItem,
    looks_like_slash_command,
)

if TYPE_CHECKING:  # pragma: no cover - typing only; never imported at runtime
    from agent.task_registry import FocusedTask, TaskRegistry
    from agent.worker_lanes import WorkerHandle, WorkerLaneRegistry

__all__ = [
    "FollowupIntent",
    "FollowupConfidence",
    "FollowupAction",
    "FollowupDecision",
    "FollowupRouter",
    "looks_like_status_query",
    "looks_like_cancel_request",
    "looks_like_new_task",
    "looks_like_correction",
    "select_target_task",
    "format_task_status",
]


# --------------------------------------------------------------------------
# Vocabulary -- plain strings on purpose (serialisable, easy to log/assert).
# --------------------------------------------------------------------------
class FollowupIntent:
    """What a follow-up message *is* (a namespace of strings, not an enum)."""

    STATUS_QUERY = "status_query"
    CANCEL_REQUEST = "cancel_request"
    APPEND = "append"
    CORRECTION = "correction"
    NEW_TASK = "new_task"
    COMMAND = "command"
    MEDIA = "media"
    AMBIGUOUS = "ambiguous"


class FollowupConfidence:
    """How sure the deterministic layer is about the classification."""

    HIGH = "high"      # an explicit signal with a single resolvable target
    MEDIUM = "medium"  # a reasonable default (sole active task, no explicit cue)
    LOW = "low"        # deferred / no usable target -- caller should decide


class FollowupAction:
    """The single safe thing :meth:`FollowupRouter.route` would do."""

    ANSWER_STATUS = "answer_status"        # report status; mutate nothing
    ATTACH_FOLLOWUP = "attach_followup"    # append the item to a task's follow-ups
    RECORD_NOTE = "record_note"            # add an append-only note (and attach item)
    REQUEST_CANCEL = "request_cancel"      # ask a known worker to cancel (cooperative)
    CREATE_TASK = "create_task"            # register a brand-new focused task
    DEFER = "defer"                        # do nothing now; needs more context
    REJECT = "reject"                      # not a task follow-up (e.g. a command)


_CONFIDENCES = frozenset(
    {FollowupConfidence.HIGH, FollowupConfidence.MEDIUM, FollowupConfidence.LOW}
)


@dataclass
class FollowupDecision:
    """The routing verdict for one :class:`PendingTurnItem`.

    ``intent`` is a :class:`FollowupIntent` value; ``confidence`` a
    :class:`FollowupConfidence` value; ``action`` a :class:`FollowupAction`
    value.  ``target_task_id`` / ``target_worker_id`` name what the action
    applies to (``None`` when not applicable -- e.g. a command boundary, a
    deferral, or a not-yet-created new task).  ``reason`` is a short machine-ish
    tag for logs/tests (``command_boundary`` / ``sole_active_task`` /
    ``multiple_active_tasks`` / ...).  ``message`` is optional user-facing text
    (a compact task status for ``answer_status``; a short confirmation for
    ``request_cancel`` / ``create_task``; ``None`` otherwise).
    """

    intent: str
    confidence: str
    action: str
    reason: str
    target_task_id: str | None = None
    target_worker_id: str | None = None
    message: str | None = None

    def __post_init__(self) -> None:
        if self.confidence not in _CONFIDENCES:
            raise ValueError(
                f"unknown confidence {self.confidence!r}; expected one of "
                f"{sorted(_CONFIDENCES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe view (every field is already a plain string or ``None``)."""
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "action": self.action,
            "reason": self.reason,
            "target_task_id": self.target_task_id,
            "target_worker_id": self.target_worker_id,
            "message": self.message,
        }


# --------------------------------------------------------------------------
# Trigger vocabulary.
#
# Each list mixes ASCII and non-ASCII entries.  ASCII entries (``stop``,
# ``how far``, ``new task``...) match on word boundaries, case-insensitively;
# non-ASCII entries (Korean) match as plain substrings.  These lists are the
# documented set, not an exhaustive grammar -- a later LLM phase refines the
# genuinely ambiguous text.
# --------------------------------------------------------------------------
_STATUS_TRIGGERS: tuple[str, ...] = (
    # Korean
    "어디까지", "어디쯤", "진행", "진행상황", "진행 상황", "상태", "상황", "언제",
    "언제 끝", "언제 나와", "언제 돼", "언제 될", "결과 언제", "다 됐", "다됐",
    "끝났", "끝나가", "얼마나 남", "얼마나 걸",
    # English
    "status", "progress", "how far", "how's it going", "hows it going",
    "how is it going", "done yet", "any update", "an update", "update on",
    "eta", "where are we", "where are you on", "is it done", "are we done",
)
_CANCEL_TRIGGERS: tuple[str, ...] = (
    # Korean -- deliberately the unambiguous imperative forms (plus bare "그만",
    # handled specially below as a whole-word match so "그만큼" does not trip it).
    "멈춰", "멈춰줘", "멈춰요", "멈춰주", "멈추세요", "멈춤",
    "중단", "중지", "취소", "취소해", "취소해줘", "취소하자",
    "그만해", "그만 해", "그만하", "그만둬", "그만 둬", "그만둘", "그만뒀",
    "그만할", "그만하자", "그만하지", "그만 좀", "때려치", "때려쳐", "관둬",
    # English
    "stop", "cancel", "abort", "cancel that", "stop it", "stop the",
    "stop that", "kill it", "kill the", "cancel the", "halt",
)
_NEW_TASK_TRIGGERS: tuple[str, ...] = (
    # Korean
    "새 작업", "새작업", "별도 작업", "별도작업", "새 태스크", "새 일",
    "다른 작업", "이건 다른", "이거 다른", "이건 별개", "별개 작업", "딴 거",
    "딴거 하나", "새로 하나",
    # English
    "new task", "separate task", "another task", "different task",
    "new request", "new job", "fresh task", "side task", "as a new task",
)
_CORRECTION_TRIGGERS: tuple[str, ...] = (
    # Korean
    "빼줘", "빼고", "빼주", "빼주세요", "빼야", "제외", "제외하고", "제외해",
    "수정", "정정", "고쳐", "고쳐줘", "바꿔", "바꿔줘", "변경", "아니",
    "아니라", "아니고", "아니야", "아닌", "그게 아니", "그거 아니", "그거 말고",
    "그게 말고", "말고", "대신", "빼지 말고",
    # English
    "not that", "remove", "instead", "correction", "correct that",
    "actually no", "actually not", "i meant", "i mean", "rather",
    "replace", "delete that", "take out", "exclude", "drop the", "leave out",
    "scratch that", "on second thought",
)

# A bare "그만" / "그만." / "그만!" used as a whole utterance is a cancel; "그만큼"
# (= "that much") is not.  Match "그만" not immediately followed by another
# Hangul syllable.
_BARE_CANCEL_RE = re.compile(r"그만(?![가-힣])")

# Conservative anti-triggers.  These are intentionally small and explicit: the
# router must not turn "don't stop" / "취소하지 마" into a cancellation, or "no new
# task" / "새 작업 말고" into task creation.  If an anti-trigger is present, the
# positive predicate returns False and classification falls through to the safer
# append/correction/ambiguous paths.
_CANCEL_NEGATION_TRIGGERS: tuple[str, ...] = (
    "don't stop", "do not stop", "dont stop", "not stop", "without stopping",
    "don't cancel", "do not cancel", "dont cancel", "not cancel",
    "don't abort", "do not abort", "dont abort", "not abort",
    "취소하지 마", "취소하지마", "취소하지 말", "취소 말고", "취소는 하지",
    "중단하지 마", "중단하지마", "중단하지 말", "중단 말고",
    "멈추지 마", "멈추지마", "멈추지 말", "멈춰 말고",
    "그만하지 마", "그만하지마", "그만하지 말",
)
_NEW_TASK_NEGATION_TRIGGERS: tuple[str, ...] = (
    "no new task", "not a new task", "don't create a new task",
    "do not create a new task", "dont create a new task",
    "don't create new task", "do not create new task", "dont create new task",
    "don't make a new task", "do not make a new task", "dont make a new task",
    "don't make new task", "do not make new task", "dont make new task",
    "not separate task", "not a separate task", "no separate task", "no new separate task",
    "not different task", "not a different task", "no different task",
    "not another task", "no another task", "don't create another task",
    "do not create another task", "dont create another task",
    "don't make another task", "do not make another task", "dont make another task",
    "do not make this a separate task", "don't make this a separate task",
    "do not make this another task", "don't make this another task",
    "do not create this as a separate task", "don't create this as a separate task",
    "새 작업 말고", "새작업 말고", "새 작업은 말고", "새작업은 말고",
    "새 작업 하지", "새작업 하지", "새 작업 아님", "새작업 아님",
    "별도 작업 말고", "별도작업 말고", "별도 작업은 말고", "별도작업은 말고",
    "다른 작업 말고", "별개 작업 말고",
)
_NEW_TASK_NEGATION_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:do\s+not|don't|dont)\s+(?:create|make)\s+"
        r"(?:this\s+)?(?:as\s+)?(?:a\s+)?(?:new|separate|another|different)\s+task\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:no|not)\s+(?:a\s+)?(?:new|separate|another|different)\s+task\b",
        re.IGNORECASE,
    ),
)

# New-task markers that, when they *lead* the message, can be peeled off so the
# created task's goal is the actual instruction rather than the marker phrase.
_NEW_TASK_LEADING: tuple[str, ...] = (
    "새 작업", "새작업", "별도 작업", "별도작업", "새 태스크", "새 일",
    "다른 작업", "별개 작업",
    "new task", "separate task", "another task", "different task",
    "new request", "new job", "fresh task", "side task",
)
_LEADING_SEPARATORS = ":-–—,.·•|>》」』]) \t　"

_ASCII_RE = re.compile(r"^[\x00-\x7f]+$")


def _is_ascii_trigger(trigger: str) -> bool:
    """True when *trigger* is pure ASCII (so it should match on word boundaries)."""
    return bool(_ASCII_RE.match(trigger))


# Per-trigger compiled word-boundary regexes, built once.  ``\b`` works for the
# multi-word ASCII phrases too ("are you \bdone yet\b?").
_ASCII_TRIGGER_RES: dict[str, re.Pattern[str]] = {}


def _ascii_trigger_re(trigger: str) -> re.Pattern[str]:
    rx = _ASCII_TRIGGER_RES.get(trigger)
    if rx is None:
        rx = re.compile(r"\b" + re.escape(trigger) + r"\b", re.IGNORECASE)
        _ASCII_TRIGGER_RES[trigger] = rx
    return rx


def _text_of(item: Any) -> str:
    """The item's text as a stripped ``str`` (``""`` when absent/non-string)."""
    text = getattr(item, "text", None)
    return text.strip() if isinstance(text, str) else ""


def _matches_any(text: str, triggers: Iterable[str]) -> bool:
    """True if *text* contains any trigger (ASCII: word-boundary; else substring)."""
    if not text:
        return False
    low = text.lower()
    for trigger in triggers:
        if _is_ascii_trigger(trigger):
            if _ascii_trigger_re(trigger).search(text):
                return True
        elif trigger in low:
            return True
    return False


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


# --------------------------------------------------------------------------
# Public predicates -- deterministic, side-effect-free, easy to unit test.
# --------------------------------------------------------------------------
def looks_like_status_query(text: Any) -> bool:
    """True when *text* reads like "where are we / when's it done / status"."""
    return _matches_any(text if isinstance(text, str) else "", _STATUS_TRIGGERS)


def looks_like_cancel_request(text: Any) -> bool:
    """True when *text* reads like an explicit "stop / cancel / 멈춰 / 취소".

    ASCII words match on boundaries (so ``stop`` matches, ``nonstop`` does not);
    Korean forms match as substrings, with a bare ``그만`` matched as a whole word
    so ``그만큼`` does not trip it.  Negated cancel phrases intentionally return
    ``False`` because an over-eager cancel is the most dangerous routing error.
    """
    if not isinstance(text, str) or not text:
        return False
    if _matches_any(text, _CANCEL_NEGATION_TRIGGERS):
        return False
    if _matches_any(text, _CANCEL_TRIGGERS):
        return True
    return bool(_BARE_CANCEL_RE.search(text))


def looks_like_new_task(text: Any) -> bool:
    """True when *text* explicitly asks for a separate / new task.

    Negated forms such as ``no new task`` / ``새 작업 말고`` return ``False`` so
    routing falls through to append/correction/ambiguous instead of creating a
    separate task by accident.
    """
    if not isinstance(text, str) or not text:
        return False
    if _matches_any(text, _NEW_TASK_NEGATION_TRIGGERS) or any(
        rx.search(text) for rx in _NEW_TASK_NEGATION_RES
    ):
        return False
    return _matches_any(text, _NEW_TASK_TRIGGERS)


def looks_like_correction(text: Any) -> bool:
    """True when *text* reads like a correction / "not that" / "instead / 빼줘".

    Deliberately broad: a misclassified append still only adds an append-only note
    and *also* attaches the item, so over-matching here is safe (whereas
    over-matching cancel is not).
    """
    return _matches_any(text if isinstance(text, str) else "", _CORRECTION_TRIGGERS)


def _strip_leading_new_task_marker(text: str) -> str:
    """Peel a leading "new task:"-style marker off *text*; else return it stripped.

    ``"새 작업: 리팩토링 해줘"`` -> ``"리팩토링 해줘"``; ``"new task - fix the bug"``
    -> ``"fix the bug"``; ``"그건 됐고 새 작업으로 X"`` -> unchanged (marker is not
    at the front); ``"새 작업"`` (marker only) -> ``"새 작업"`` (so the goal is not
    empty).
    """
    s = text.lstrip()
    low = s.lower()
    for marker in _NEW_TASK_LEADING:
        if low.startswith(marker.lower()):
            rest = s[len(marker) :].lstrip(_LEADING_SEPARATORS).strip()
            return rest if rest else text.strip()
    return text.strip()


def select_target_task(
    active_tasks: Iterable["FocusedTask"],
    *,
    task_hint: str | None = None,
) -> "FocusedTask | None":
    """Pick the one task a follow-up unambiguously applies to, else ``None``.

    With a non-empty *task_hint*: the active task whose ``task_id`` equals the
    hint, or ``None`` if the hint matches nothing in *active_tasks* (a named-but-
    absent target is *not* silently reinterpreted as "the other one").  With no
    hint: the sole active task, or ``None`` when there are zero or several.
    """
    tasks = list(active_tasks)
    if task_hint:
        for task in tasks:
            if getattr(task, "task_id", None) == task_hint:
                return task
        return None
    if len(tasks) == 1:
        return tasks[0]
    return None


def format_task_status(
    task: "FocusedTask",
    *,
    worker_handle: "WorkerHandle | None" = None,
) -> str:
    """A compact one-line status string for *task* (optionally with a live worker).

    e.g. ``"task task-ab12 [running]: draft the briefing · 2 follow-ups queued ·
    worker worker-9f [running]"``.
    """
    task_id = getattr(task, "task_id", "?")
    status = getattr(task, "status", "?")
    parts: list[str] = [f"task {task_id} [{status}]"]
    raw_goal = getattr(task, "user_goal", None)
    goal = raw_goal.strip() if isinstance(raw_goal, str) else ""
    if goal:
        parts.append(f": {_truncate(goal, 140)}")
    pending = getattr(task, "pending_followups", None) or []
    n = len(pending)
    if n:
        parts.append(f" · {n} follow-up{'s' if n != 1 else ''} queued")
    if worker_handle is not None:
        wid = getattr(worker_handle, "worker_id", None)
        wstatus = getattr(worker_handle, "status", None)
        if wid:
            parts.append(f" · worker {wid} [{wstatus}]")
            if getattr(worker_handle, "cancel_requested", False):
                parts.append(" (cancel requested)")
    else:
        wid = getattr(task, "active_worker_id", None)
        if wid:
            kind = getattr(task, "worker_kind", None)
            parts.append(f" · worker {wid}" + (f" ({kind})" if kind else ""))
    return "".join(parts)


# --------------------------------------------------------------------------
# FollowupRouter
# --------------------------------------------------------------------------
class FollowupRouter:
    """Deterministic, conservative follow-up classifier + safe-mutation router.

    Stateless today (constructed with no arguments); kept as a class so a later
    phase can give it configuration -- custom trigger vocabularies, a policy
    profile, an LLM fallback -- without changing call sites.
    """

    # -- classification (pure) -------------------------------------------
    def classify(
        self,
        item: PendingTurnItem,
        *,
        active_tasks: Iterable["FocusedTask"] | None = None,
    ) -> FollowupDecision:
        """Classify *item* against the *active_tasks* of its session.  Mutates nothing.

        Order of decision (first that applies wins):

        1. command boundary  -> ``COMMAND`` / ``reject`` (never folded into task text)
        2. media / attachment / control payload -> ``MEDIA`` (attach to the sole
           active task, else defer) -- the original :class:`PendingTurnItem` is
           preserved, never flattened to plain text
        3. explicit "new task" phrasing -> ``NEW_TASK`` / ``create_task``
        4. explicit "stop / cancel" -> ``CANCEL_REQUEST`` / ``request_cancel``
           (only with a single resolvable target; never fans out)
        5. "status / when / how far" -> ``STATUS_QUERY`` / ``answer_status``
           (only with a single resolvable target; mutates nothing)
        6. "not that / instead / 빼줘" -> ``CORRECTION`` / ``record_note``
           (only with a single resolvable target; append-only)
        7. otherwise plain text -> ``APPEND`` / ``attach_followup`` to the sole
           active task
        8. anything with zero or several candidate tasks and no usable hint ->
           ``AMBIGUOUS`` / ``defer``
        """
        tasks = list(active_tasks or ())
        hint = getattr(item, "task_hint", None) or None
        text = _text_of(item)
        kind = getattr(item, "kind", None)
        boundary = getattr(item, "boundary", None)

        # 1. Command boundary -- a wall; never attach to task text.
        if (
            kind == KIND_COMMAND
            or boundary == BOUNDARY_COMMAND
            or looks_like_slash_command(getattr(item, "text", None))
        ):
            return FollowupDecision(
                intent=FollowupIntent.COMMAND,
                confidence=FollowupConfidence.HIGH,
                action=FollowupAction.REJECT,
                reason="command_boundary",
            )

        # 2. Media / attachment / opaque control payload -- preserve as-is.
        has_media = bool(getattr(item, "media_refs", None)) or bool(
            getattr(item, "has_media", False)
        )
        if has_media or kind in (KIND_MEDIA, KIND_ATTACHMENT, KIND_CONTROL):
            target = select_target_task(tasks, task_hint=hint)
            if target is not None:
                return FollowupDecision(
                    intent=FollowupIntent.MEDIA,
                    confidence=(
                        FollowupConfidence.HIGH if hint else FollowupConfidence.MEDIUM
                    ),
                    action=FollowupAction.ATTACH_FOLLOWUP,
                    reason="hinted_task" if hint else "sole_active_task",
                    target_task_id=getattr(target, "task_id", None),
                )
            return FollowupDecision(
                intent=FollowupIntent.MEDIA,
                confidence=FollowupConfidence.LOW,
                action=FollowupAction.DEFER,
                reason=self._no_target_reason(tasks, hint),
            )

        # 3. Explicit "new task" -- create regardless of how many tasks exist.
        if looks_like_new_task(text):
            return FollowupDecision(
                intent=FollowupIntent.NEW_TASK,
                confidence=FollowupConfidence.HIGH,
                action=FollowupAction.CREATE_TASK,
                reason="explicit_new_task",
            )

        # 4. Explicit cancel -- only with a single resolvable target.
        if looks_like_cancel_request(text):
            target = select_target_task(tasks, task_hint=hint)
            if target is not None:
                worker_id = getattr(target, "active_worker_id", None)
                return FollowupDecision(
                    intent=FollowupIntent.CANCEL_REQUEST,
                    confidence=FollowupConfidence.HIGH,
                    action=FollowupAction.REQUEST_CANCEL,
                    reason="hinted_task" if hint else "sole_active_task",
                    target_task_id=getattr(target, "task_id", None),
                    target_worker_id=worker_id,
                )
            return FollowupDecision(
                intent=FollowupIntent.AMBIGUOUS,
                confidence=FollowupConfidence.LOW,
                action=FollowupAction.DEFER,
                reason="cancel_" + self._no_target_reason(tasks, hint),
            )

        # 5. Status query -- only with a single resolvable target; never mutates.
        if looks_like_status_query(text):
            target = select_target_task(tasks, task_hint=hint)
            if target is not None:
                return FollowupDecision(
                    intent=FollowupIntent.STATUS_QUERY,
                    confidence=FollowupConfidence.HIGH,
                    action=FollowupAction.ANSWER_STATUS,
                    reason="hinted_task" if hint else "sole_active_task",
                    target_task_id=getattr(target, "task_id", None),
                    target_worker_id=getattr(target, "active_worker_id", None),
                    message=format_task_status(target),
                )
            return FollowupDecision(
                intent=FollowupIntent.AMBIGUOUS,
                confidence=FollowupConfidence.LOW,
                action=FollowupAction.DEFER,
                reason="status_" + self._no_target_reason(tasks, hint),
            )

        # 6. Correction -- only with a single resolvable target; append-only.
        if looks_like_correction(text):
            target = select_target_task(tasks, task_hint=hint)
            if target is not None:
                return FollowupDecision(
                    intent=FollowupIntent.CORRECTION,
                    confidence=FollowupConfidence.HIGH,
                    action=FollowupAction.RECORD_NOTE,
                    reason="hinted_task" if hint else "sole_active_task",
                    target_task_id=getattr(target, "task_id", None),
                )
            return FollowupDecision(
                intent=FollowupIntent.AMBIGUOUS,
                confidence=FollowupConfidence.LOW,
                action=FollowupAction.DEFER,
                reason="correction_" + self._no_target_reason(tasks, hint),
            )

        # 7. Plain text follow-up -- the safe default: attach to the sole task.
        target = select_target_task(tasks, task_hint=hint)
        if target is not None:
            return FollowupDecision(
                intent=FollowupIntent.APPEND,
                confidence=(
                    FollowupConfidence.HIGH if hint else FollowupConfidence.MEDIUM
                ),
                action=FollowupAction.ATTACH_FOLLOWUP,
                reason="hinted_task" if hint else "sole_active_task",
                target_task_id=getattr(target, "task_id", None),
            )

        # 8. Nothing safe to do -- defer, do not guess.
        return FollowupDecision(
            intent=FollowupIntent.AMBIGUOUS,
            confidence=FollowupConfidence.LOW,
            action=FollowupAction.DEFER,
            reason=self._no_target_reason(tasks, hint),
        )

    @staticmethod
    def _no_target_reason(tasks: list["FocusedTask"], hint: str | None) -> str:
        if hint:
            return "task_hint_not_active"
        if not tasks:
            return "no_active_task"
        return "multiple_active_tasks"

    # -- routing (performs the one safe mutation) ------------------------
    def route(
        self,
        item: PendingTurnItem,
        *,
        task_registry: "TaskRegistry",
        worker_registry: "WorkerLaneRegistry | None" = None,
        session_key: str | None = None,
    ) -> FollowupDecision:
        """Classify *item* against *task_registry* and perform the safe mutation.

        Active tasks are taken from ``task_registry.list_tasks(session_key=...,
        active_only=True)`` -- *session_key* if given, else ``item.session_key``
        (and ``None`` means "across all sessions", so a lone active task is still
        found).  Then, by action:

        * ``attach_followup`` -- ``task_registry.attach_followup(task_id, item)``
          (the *same* :class:`PendingTurnItem`; ``raw`` is never touched).
        * ``record_note`` -- ``task_registry.add_note(task_id, "correction: ...")``
          **and** ``attach_followup(task_id, item)``; the task ``user_goal`` is
          not modified.
        * ``request_cancel`` -- if a *worker_registry* is supplied and the target
          worker id is known, ``worker_registry.cancel(worker_id)`` (cooperative;
          a missing worker is a safe no-op).  No task is force-cancelled here.
        * ``create_task`` -- ``task_registry.create_task(goal, session_key=...,
          origin=...)`` with a leading "new task:"-style marker peeled off the
          goal; the decision's ``target_task_id`` / ``message`` are filled in.
        * ``answer_status`` -- mutates nothing; refreshes ``message`` with a live
          worker handle when one is reachable via *worker_registry*.
        * ``defer`` / ``reject`` -- mutates nothing.

        The returned :class:`FollowupDecision` is the (possibly message-enriched)
        verdict.  ``route`` does not de-duplicate: calling it twice with the same
        item attaches it twice.
        """
        effective_sk = (
            session_key if session_key is not None else getattr(item, "session_key", None)
        )
        active_tasks = list(
            task_registry.list_tasks(session_key=effective_sk, active_only=True)
        )
        decision = self.classify(item, active_tasks=active_tasks)
        action = decision.action

        if action == FollowupAction.ATTACH_FOLLOWUP and decision.target_task_id:
            task_registry.attach_followup(decision.target_task_id, item)
            return decision

        if action == FollowupAction.RECORD_NOTE and decision.target_task_id:
            body = _text_of(item)
            note = "correction: " + _truncate(body, 280) if body else "correction (no text)"
            task_registry.add_note(decision.target_task_id, note)
            task_registry.attach_followup(decision.target_task_id, item)
            return decision

        if action == FollowupAction.REQUEST_CANCEL:
            worker_id = decision.target_worker_id
            if worker_registry is not None and worker_id:
                try:
                    cancelled = bool(worker_registry.cancel(worker_id))
                except KeyError:
                    cancelled = False
                if cancelled:
                    return replace(
                        decision,
                        message=f"requested cancellation of worker {worker_id}",
                    )
                return replace(
                    decision,
                    message=f"worker {worker_id} was not active; nothing cancelled",
                )
            # No worker registry / no known worker: the decision still records the
            # cancel intent + target; a higher layer (or the user) follows up.
            return decision

        if action == FollowupAction.CREATE_TASK:
            goal = _strip_leading_new_task_marker(_text_of(item)) or "(untitled task)"
            origin = self._origin_from_item(item, effective_sk)
            try:
                new_task = task_registry.create_task(
                    goal, session_key=effective_sk, origin=origin
                )
            except TypeError:
                # A registry whose create_task does not take an ``origin`` kwarg.
                new_task = task_registry.create_task(goal, session_key=effective_sk)
            return replace(
                decision,
                target_task_id=getattr(new_task, "task_id", None),
                message=f"created task {getattr(new_task, 'task_id', '?')}: {_truncate(goal, 140)}",
            )

        if action == FollowupAction.ANSWER_STATUS and decision.target_task_id:
            task = task_registry.get_task(decision.target_task_id)
            if task is not None:
                handle = None
                worker_id = getattr(task, "active_worker_id", None)
                if worker_registry is not None and worker_id:
                    try:
                        handle = worker_registry.status(worker_id)
                    except KeyError:
                        handle = None
                return replace(
                    decision, message=format_task_status(task, worker_handle=handle)
                )
            return decision

        # answer_status without a target, defer, reject: nothing to mutate.
        return decision

    @staticmethod
    def _origin_from_item(item: PendingTurnItem, session_key: str | None) -> dict[str, Any]:
        """A plain-dict origin built from a :class:`PendingTurnItem` for a new task.

        Only the provenance the item actually carries (its ``source`` platform
        and ``session_key``) is used; a dict is returned so the registry can lift
        it without this module importing :class:`~agent.task_registry.TaskOrigin`.
        """
        source = getattr(item, "source", None)
        return {
            "platform": source if isinstance(source, str) and source else None,
            "session_key": session_key
            if session_key is not None
            else getattr(item, "session_key", None),
        }
