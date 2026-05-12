/**
 * Helpers for `display.busy_input_mode: integrated` (CLI parity).
 *
 * Under `integrated` mode, plain-text fragments typed while Hermes is busy are
 * queued (never interrupt the running turn) and then, when the turn settles,
 * the leading run of consecutive plain-text fragments is coalesced into ONE
 * follow-up prompt — wrapped with an explicit "arrived while busy" preamble — so
 * the model produces a single integrated answer instead of one reply per
 * fragment. Slash-command queue entries (e.g. via `/queue add /foo`) are a hard
 * boundary: they end the run and are left in place so they are not folded into
 * model text. File/media-drop-looking entries are also a hard boundary so the
 * normal send path can run input.detect_drop before submitting them. This mirrors
 * `cli.HermesCLI._coalesce_pending_integrated_busy_queue` / `_format_integrated_busy_input`.
 *
 * On top of that boundary coalescing, `resolveIntegratedQueueDrain` adds a thin
 * *lifecycle* layer: a busy-time fragment is often a duplicate of something the
 * turn that just settled already produced (a "todo는?" right after the assistant
 * printed the TODO recap, an "iterate on that" after it laid out the iteration
 * plan). Without acknowledgement those stale items drain on settle and trigger a
 * second full answer. The resolver consumes such covered duplicates, always
 * keeps status queries (live status drifts — a queued "끝났어?" still deserves a
 * fresh delta), and coalesces the genuinely-new survivors as before. See
 * `agent/followup_router.py` for the server-side intent vocabulary this mirrors.
 */
import { looksLikeSlashCommand } from '../domain/slash.js'
import type { Msg } from '../types.js'

import { startsLikeDroppedPathCandidate } from './dropPath.js'

/** Preamble prepended to coalesced busy-time fragments. Mirrors the CLI text. */
export const INTEGRATED_BUSY_PREAMBLE =
  'Additional user input arrived while Hermes was working ' +
  '(captured under /busy integrated). Integrate the following ' +
  'follow-up with the task or result you were just producing. Do not ' +
  'treat it as an unrelated new topic unless the user clearly asks.\n\n'

/** Wrap coalesced busy-time fragment text as an integrated follow-up prompt. */
export const formatIntegratedBusyInput = (text: string): string => `${INTEGRATED_BUSY_PREAMBLE}${text}`

export interface IntegratedDrain {
  /** The prompt to submit now (a wrapped follow-up), or undefined if nothing coalescible was at the front. */
  readonly send: string | undefined
  /** Items left in the queue, in original order (starts at the first slash-command boundary, if any). */
  readonly remaining: readonly string[]
}

/** First index in *queue* that is a hard boundary (slash command / file-drop-looking entry), else `queue.length`. */
const firstBoundaryIndex = (queue: readonly string[]): number => {
  let i = 0

  while (i < queue.length && !looksLikeSlashCommand(queue[i]!) && !startsLikeDroppedPathCandidate(queue[i]!)) {
    i++
  }

  return i
}

/**
 * Pop the leading run of non-slash-command fragments from *queue* and return
 * them coalesced+wrapped as a single follow-up prompt. The first slash-command
 * entry (or a file/media-drop-looking entry) and everything after it is left in
 * `remaining`. Empty/whitespace-only fragments are skipped when joining but
 * still consumed. If the queue is empty or starts with a slash command/file drop,
 * `send` is undefined and `remaining` is the queue unchanged (callers fall back
 * to their normal one-item dequeue).
 */
export const coalesceIntegratedQueue = (queue: readonly string[]): IntegratedDrain => {
  const i = firstBoundaryIndex(queue)

  if (i === 0) {
    return { remaining: queue, send: undefined }
  }

  const run = queue.slice(0, i)
  const remaining = queue.slice(i)

  const joined = run
    .map(s => s.trim())
    .filter(s => s.length > 0)
    .join('\n\n')

  if (!joined) {
    // The run was all blanks — consume it but emit nothing to send.
    return { remaining, send: undefined }
  }

  return { remaining, send: formatIntegratedBusyInput(joined) }
}

// ---------------------------------------------------------------------------
// Queue-drain lifecycle resolution (integrated mode).
// ---------------------------------------------------------------------------

/**
 * Coarse intent of a single busy-time follow-up fragment. Deliberately tiny and
 * conservative — a first cut, not a grammar. `'status'` fragments are *never*
 * suppressed by the resolver (live status drifts); only `'todo'` / `'iteration'`
 * fragments are eligible to be consumed, and only when the just-finished
 * response actually covered that kind. Generic steering classifies as `null`
 * and is always preserved. The product-correct successor is a structured
 * `{ id, text, lifecycle }` queue where the agent records per-item
 * acknowledgement instead of this after-the-fact heuristic.
 */
export type FollowupFragmentIntent = 'iteration' | 'status' | 'todo' | null

// Trigger vocabularies. Korean entries match as plain substrings (Hangul has no
// useful word boundary — same trade-off as agent/followup_router.py); ASCII
// entries match as case-insensitive substrings too.
//
// Stray-match consequences differ by bucket, so the buckets are sized
// accordingly: a `status` stray match is harmless (status fragments are *never*
// suppressed), but a `todo` / `iteration` stray match is *consumed* — silently
// dropped, never replayed — whenever the just-finished response covered that
// kind. So `TODO_TRIGGERS` / `ITERATION_TRIGGERS` lean on tokens that are
// distinctive *as steering would phrase a recap request* and avoid bare words
// that routinely appear in genuinely-new steering: notably no plain `'to do'`
// (it lives inside "want to do …", "how to do …" — that fragment is steering, not
// a TODO recap; the literal noun "todo"/"to-do" and "task list" still match).
// Likewise the status list omits bare ambiguous Korean words ("진행" alone can
// mean "go ahead", "오늘" can be steering) — only unambiguous status phrasings.
const STATUS_TRIGGERS: readonly string[] = [
  // Korean — "is it done / what are you doing / progress / how much is left / when"
  '끝났',
  '끝나가',
  '끝나니',
  '끝나면',
  '끝났나',
  '다 됐',
  '다됐',
  '다 돼',
  '다돼',
  '지금 뭐',
  '뭐 하고 있',
  '뭐하고있',
  '뭐 하는 중',
  '뭐하는중',
  '뭐 함',
  '뭐함',
  '진행 상황',
  '진행상황',
  '진행 중',
  '진행중',
  '진행돼',
  '진행 돼',
  '진행되',
  '얼마나 남',
  '얼마나 됐',
  '얼마나 걸',
  '어디까지',
  '어디쯤',
  '언제 끝',
  '언제 돼',
  '언제 나와',
  // English
  'status',
  'progress',
  'done yet',
  'how far',
  'eta',
  'any update',
  'update on',
  'where are we',
  'how is it going',
  "how's it going",
  'working on'
]

const TODO_TRIGGERS: readonly string[] = [
  // The noun, hyphenated or not — but *not* bare 'to do' (that is a verb phrase
  // inside ordinary steering: "want to do…", "how to do…"). 'task list' covers
  // the explicit-list phrasing without the false positives.
  'todo',
  'todos',
  'to-do',
  'to-dos',
  'task list',
  'tasklist',
  '할일',
  '할 일',
  '해야 할 일',
  '해야할일',
  '투두',
  '투 두'
]

const ITERATION_TRIGGERS: readonly string[] = [
  'iteration',
  'iterations',
  'iterate',
  'iterating',
  'reiterate',
  'simulation',
  'simulate',
  'simulating',
  '시뮬레이션',
  '시뮬레',
  '시뮬',
  '이터레이션',
  '반복'
]

// How a `coveredIntents` tag (e.g. `'todo_summary'`, `'iteration_plan'`,
// `'worker_status'`) maps onto a fragment kind that becomes eligible for
// suppression. `'status'` tags map to nothing on purpose — see above.
const TODO_COVER_HINTS: readonly string[] = ['todo', '할일', '할 일', 'agenda', 'task_list', 'tasklist']
const ITERATION_COVER_HINTS: readonly string[] = ['iteration', 'iterate', 'simulation', 'simul', '반복']

const includesAny = (haystack: string, needles: readonly string[]): boolean => needles.some(n => haystack.includes(n))

/**
 * Best-effort intent of a queued busy-time fragment. Status is checked first so
 * a fragment that asks about a covered topic ("todo 끝났어?") survives as a
 * status query rather than being suppressed as a covered TODO duplicate.
 */
export const classifyFollowupFragment = (text: string): FollowupFragmentIntent => {
  const low = text.trim().toLowerCase()

  if (!low) {
    return null
  }

  if (includesAny(low, STATUS_TRIGGERS)) {
    return 'status'
  }

  if (includesAny(low, TODO_TRIGGERS)) {
    return 'todo'
  }

  if (includesAny(low, ITERATION_TRIGGERS)) {
    return 'iteration'
  }

  return null
}

/** The set of fragment kinds the just-finished response covered (a subset of {`'todo'`, `'iteration'`}). */
const coveredFragmentKinds = (coveredIntents: readonly string[]): ReadonlySet<'iteration' | 'todo'> => {
  const kinds = new Set<'iteration' | 'todo'>()

  for (const raw of coveredIntents) {
    const tag = raw.toLowerCase()

    if (includesAny(tag, TODO_COVER_HINTS)) {
      kinds.add('todo')
    }

    if (includesAny(tag, ITERATION_COVER_HINTS)) {
      kinds.add('iteration')
    }
  }

  return kinds
}

/** Context about the turn that just settled, used to recognise covered duplicates. */
export interface RecentResponseContext {
  /**
   * Intent tags the just-finished assistant response already addressed — e.g.
   * `'todo_summary'`, `'iteration_plan'`, `'worker_status'`. A queued fragment
   * matching one of these (other than a status query) is consumed by the drain
   * rather than replayed as a second full answer. Defaults to none, in which
   * case the drain is exactly the previous boundary coalescing.
   */
  readonly coveredIntents?: readonly string[]
}

/** What `resolveIntegratedQueueDrain` decided about a settle-time queue drain. */
export interface IntegratedQueueResolution {
  /**
   * Leading-run fragments dropped because the response already covered their
   * intent — neither sent now nor re-queued. (`consumed` + `unresolved` together
   * partition the leading run.)
   */
  readonly consumed: readonly string[]
  /** Leading-run fragments that survived suppression, in original order — the fragments `send` is built from (blanks among them are skipped when building `send`). */
  readonly unresolved: readonly string[]
  /** The single wrapped follow-up prompt to submit now (built from `unresolved`), or undefined when nothing coalescible survived. */
  readonly send: string | undefined
  /** Items left in the queue after the drain, in original order — the first slash/file-drop boundary entry onward (callers fall back to their normal one-item dequeue on these). */
  readonly remaining: readonly string[]
}

/**
 * Resolve a settle-time integrated-busy queue drain.
 *
 * Walks the leading run of non-boundary fragments (a slash command or a
 * file/media-drop-looking entry is a hard wall — never crossed, never
 * semantically filtered) and, against *context*, splits it into:
 *
 * - `consumed` — fragments whose intent the just-finished response already
 *   covered (a covered TODO/iteration recap duplicate); dropped silently.
 * - `unresolved` — everything else in the run, including every status query
 *   (live status drifts, so a queued "끝났어?" always survives) and all generic
 *   steering; these are coalesced+wrapped into `send`.
 *
 * `remaining` is the boundary entry onward (unchanged). With no `coveredIntents`
 * this is exactly {@link coalesceIntegratedQueue} plus an empty `consumed`.
 */
export const resolveIntegratedQueueDrain = (
  queue: readonly string[],
  context: RecentResponseContext = {}
): IntegratedQueueResolution => {
  const i = firstBoundaryIndex(queue)
  const run = queue.slice(0, i)

  if (run.length === 0) {
    // Queue is empty or starts with a slash command / file-drop boundary —
    // nothing to coalesce or resolve; let the caller one-item dequeue.
    return { consumed: [], remaining: queue, send: undefined, unresolved: [] }
  }

  const coveredKinds = coveredFragmentKinds(context.coveredIntents ?? [])

  const consumed: string[] = []
  const unresolved: string[] = []

  if (coveredKinds.size > 0) {
    for (const fragment of run) {
      const intent = classifyFollowupFragment(fragment)

      // Only covered TODO/iteration recaps are eligible for suppression; status
      // queries and generic steering always survive.
      if ((intent === 'todo' || intent === 'iteration') && coveredKinds.has(intent)) {
        consumed.push(fragment)
      } else {
        unresolved.push(fragment)
      }
    }
  } else {
    unresolved.push(...run)
  }

  if (consumed.length === 0) {
    // No covered duplicates — identical to plain boundary coalescing.
    const coalesced = coalesceIntegratedQueue(queue)

    return { consumed: [], remaining: coalesced.remaining, send: coalesced.send, unresolved: run }
  }

  // Re-wrap only the survivors; the boundary tail (queue.slice(i)) is untouched.
  const { send } = coalesceIntegratedQueue(unresolved)

  return { consumed, remaining: queue.slice(i), send, unresolved }
}

// ---------------------------------------------------------------------------
// Settle-time coverage signal — the runtime input to `resolveIntegratedQueueDrain`.
// ---------------------------------------------------------------------------

/**
 * Best-effort `coveredIntents` for the turn that just settled, derived from the
 * transcript the TUI already holds (`historyItems`) — no new agent/gateway
 * signal required.
 *
 * The one coverage fact the TUI records *structurally* today is the archived
 * TODO trail: when a turn used the TODO tool, `archiveDoneTodos()` appends a
 * `kind: 'trail'` history item carrying that turn's `todos` at `message.complete`.
 * That item *is* the on-screen TODO recap, so a busy-time "todo는?" / "할 일?"
 * fragment queued during that turn is already answered — `resolveIntegratedQueueDrain`
 * consumes it instead of replaying a second full answer. Turns that tracked no
 * TODOs produce no such item, so a queued "todo?" there still drains and gets a
 * fresh answer (correct — nothing covered it).
 *
 * Scope is strictly the just-completed turn: scan back from the end of `history`
 * and stop at the most recent user message — a TODO trail from an older turn must
 * not suppress a freshly-queued recap. Iteration-plan and worker-status recaps
 * have no comparable structured artifact in the TUI yet, so they are deliberately
 * *not* inferred from prose here (that needs an agent-emitted per-turn coverage
 * set — see the `TODO(integrated-queue-lifecycle)` in `useMainApp.ts`); status
 * queries are never suppressed by the resolver regardless of what this returns.
 */
export const coveredIntentsFromSettledTurn = (history: readonly Msg[]): string[] => {
  for (let i = history.length - 1; i >= 0; i--) {
    const msg = history[i]!

    // Reached the prompt that started this turn — anything earlier belongs to a
    // prior turn and must not count as coverage for what was queued during this one.
    if (msg.role === 'user') {
      break
    }

    if (msg.kind === 'trail' && msg.todos !== undefined && msg.todos.length > 0) {
      return ['todo_summary']
    }
  }

  return []
}
