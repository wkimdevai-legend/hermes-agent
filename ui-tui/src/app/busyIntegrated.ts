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
 */
import { looksLikeSlashCommand } from '../domain/slash.js'

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
  let i = 0
  while (i < queue.length && !looksLikeSlashCommand(queue[i]!) && !startsLikeDroppedPathCandidate(queue[i]!)) {
    i++
  }

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
