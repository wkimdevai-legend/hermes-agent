import { describe, expect, it } from 'vitest'

import {
  classifyFollowupFragment,
  coalesceIntegratedQueue,
  coveredIntentsFromSettledTurn,
  formatIntegratedBusyInput,
  INTEGRATED_BUSY_PREAMBLE,
  resolveIntegratedQueueDrain
} from '../app/busyIntegrated.js'
import type { Msg } from '../types.js'

describe('formatIntegratedBusyInput', () => {
  it('prefixes the integrated-busy preamble', () => {
    const out = formatIntegratedBusyInput('첫 번째\n\n두 번째')

    expect(out.startsWith(INTEGRATED_BUSY_PREAMBLE)).toBe(true)
    expect(out.endsWith('첫 번째\n\n두 번째')).toBe(true)
    // The preamble must tell the model NOT to treat this as a fresh topic.
    expect(out).toMatch(/Integrate the following/i)
    expect(out).toMatch(/unrelated new topic/i)
  })
})

describe('coalesceIntegratedQueue', () => {
  it('coalesces a run of plain-text fragments into ONE wrapped follow-up', () => {
    const { send, remaining } = coalesceIntegratedQueue(['첫 번째 후속', '두 번째 후속', '이건 같은 맥락'])

    expect(remaining).toEqual([])
    expect(send).toBe(formatIntegratedBusyInput('첫 번째 후속\n\n두 번째 후속\n\n이건 같은 맥락'))
    // It must NOT be the naive "first item only" / sequential behavior.
    expect(send).not.toBe('첫 번째 후속')
  })

  it('still wraps a single queued fragment (CLI parity)', () => {
    const { send, remaining } = coalesceIntegratedQueue(['하나만'])

    expect(remaining).toEqual([])
    expect(send).toBe(formatIntegratedBusyInput('하나만'))
  })

  it('stops the run at a slash-command queue entry and leaves it (and the rest) in place', () => {
    const { send, remaining } = coalesceIntegratedQueue(['처음', '추가', '/busy status', '나중'])

    expect(send).toBe(formatIntegratedBusyInput('처음\n\n추가'))
    expect(remaining).toEqual(['/busy status', '나중'])
  })

  it('does not coalesce when the queue starts with a slash command', () => {
    const q = ['/busy status', '나중']
    const { send, remaining } = coalesceIntegratedQueue(q)

    expect(send).toBeUndefined()
    // remaining is the queue unchanged so the caller falls back to a one-item dequeue.
    expect(remaining).toBe(q)
  })

  it('stops the run at file/media-drop-looking entries so normal detection can handle them', () => {
    for (const pathish of ['/Users/me/notes', '~/notes.md', '"./notes.md"', "'../notes.md'", 'file:///tmp/a.png']) {
      const { send, remaining } = coalesceIntegratedQueue(['처음', '추가', pathish, '나중'])

      expect(send).toBe(formatIntegratedBusyInput('처음\n\n추가'))
      expect(remaining).toEqual([pathish, '나중'])
    }
  })

  it('does not coalesce when the queue starts with a file/media-drop-looking entry', () => {
    const q = ['/Users/me/notes', '나중']
    const { send, remaining } = coalesceIntegratedQueue(q)

    expect(send).toBeUndefined()
    expect(remaining).toBe(q)
  })

  it('skips blank fragments when joining but still consumes them', () => {
    const { send, remaining } = coalesceIntegratedQueue(['   ', 'real', '\n', '/x', 'after'])

    expect(send).toBe(formatIntegratedBusyInput('real'))
    expect(remaining).toEqual(['/x', 'after'])
  })

  it('returns an empty queue unchanged', () => {
    const q: string[] = []
    const { send, remaining } = coalesceIntegratedQueue(q)

    expect(send).toBeUndefined()
    expect(remaining).toBe(q)
  })
})

describe('classifyFollowupFragment', () => {
  it('tags a TODO-recap fragment as todo', () => {
    expect(classifyFollowupFragment('todo 는 어떻게 되고? 오늘?')).toBe('todo')
    expect(classifyFollowupFragment('todo는?')).toBe('todo')
    expect(classifyFollowupFragment('할 일 목록 다시 보여줘')).toBe('todo')
  })

  it('tags an iteration/meta instruction as iteration', () => {
    expect(classifyFollowupFragment('그런 거를 iteration 하라고 하는 거잖아')).toBe('iteration')
    expect(classifyFollowupFragment('시뮬레이션 다시 돌려줘')).toBe('iteration')
  })

  it('tags a "is it done / what are you doing" query as status', () => {
    expect(classifyFollowupFragment('끝났어?')).toBe('status')
    expect(classifyFollowupFragment('지금 뭐 하고 있어?')).toBe('status')
    expect(classifyFollowupFragment('any update on this?')).toBe('status')
  })

  it('checks status before todo so a status-shaped question about a covered topic survives', () => {
    expect(classifyFollowupFragment('todo 끝났어?')).toBe('status')
  })

  it('leaves genuinely new steering unclassified (null)', () => {
    expect(classifyFollowupFragment('zanu 비교도 꼭 같이 봐줘')).toBeNull()
    expect(classifyFollowupFragment('KOL용으로')).toBeNull()
    expect(classifyFollowupFragment('짧게')).toBeNull()
    expect(classifyFollowupFragment('')).toBeNull()
  })

  it('does not classify a steering fragment that merely contains the verb phrase "to do" as a TODO recap', () => {
    // These are new instructions, not "show me the TODO list" — the bare 'to do'
    // substring must not pull them into the suppressible `todo` bucket. The noun
    // "todo"/"to-do" and "task list" still classify (covered above).
    expect(classifyFollowupFragment('I want to do the zanu comparison too')).toBeNull()
    expect(classifyFollowupFragment('also show me how to do the KOL version')).toBeNull()
    expect(classifyFollowupFragment('remind me what to do next')).toBeNull()
  })
})

describe('resolveIntegratedQueueDrain', () => {
  it('is a no-op pass-through when no intents were covered (matches coalesceIntegratedQueue)', () => {
    const cases: string[][] = [
      ['첫 후속', '두 후속'],
      ['처음', '추가', '/busy status', '나중'],
      ['/busy status', '나중'],
      ['   ', 'real', '\n', '/x', 'after'],
      []
    ]

    for (const q of cases) {
      const base = coalesceIntegratedQueue(q)
      const resolved = resolveIntegratedQueueDrain(q)

      expect(resolved.send).toBe(base.send)
      expect(resolved.remaining).toEqual(base.remaining)
      expect(resolved.consumed).toEqual([])
    }

    // An explicit-but-empty context behaves the same.
    const empty = resolveIntegratedQueueDrain(['첫 후속'], { coveredIntents: [] })

    expect(empty.send).toBe(formatIntegratedBusyInput('첫 후속'))
    expect(empty.consumed).toEqual([])
    expect(empty.unresolved).toEqual(['첫 후속'])
  })

  it('consumes queued text whose intent was already covered by the current response', () => {
    const r = resolveIntegratedQueueDrain(['todo 는 어떻게 되고? 오늘?'], { coveredIntents: ['todo_summary'] })

    expect(r.consumed).toEqual(['todo 는 어떻게 되고? 오늘?'])
    expect(r.unresolved).toEqual([])
    // No second wrapped prompt is produced for the covered duplicate.
    expect(r.send).toBeUndefined()
    expect(r.remaining).toEqual([])
  })

  it('consumes duplicate iteration instructions already covered by the current response', () => {
    const r = resolveIntegratedQueueDrain(['그런 거를 iteration 하라고 하는 거잖아'], {
      coveredIntents: ['iteration_plan']
    })

    expect(r.consumed).toEqual(['그런 거를 iteration 하라고 하는 거잖아'])
    expect(r.send).toBeUndefined()
    expect(r.remaining).toEqual([])
  })

  it('preserves status queries because they may need a fresh delta', () => {
    for (const probe of ['끝났어?', '지금 뭐 하고 있어?']) {
      // Even when worker_status (and everything else) was already covered, a
      // queued status query must survive — it gets a fresh answer, not silence.
      const r = resolveIntegratedQueueDrain([probe], {
        coveredIntents: ['worker_status', 'todo_summary', 'iteration_plan']
      })

      expect(r.consumed).toEqual([])
      expect(r.unresolved).toEqual([probe])
      expect(r.send).toBe(formatIntegratedBusyInput(probe))
    }
  })

  it('preserves new steering after filtering a covered TODO duplicate (mixed bundle)', () => {
    const r = resolveIntegratedQueueDrain(['todo는?', 'zanu 비교', '짧게'], { coveredIntents: ['todo_summary'] })

    expect(r.consumed).toEqual(['todo는?'])
    expect(r.unresolved).toEqual(['zanu 비교', '짧게'])
    expect(r.send).toBe(formatIntegratedBusyInput('zanu 비교\n\n짧게'))
    expect(r.remaining).toEqual([])
  })

  it('preserves brand-new steering fragments verbatim even when other intents were covered', () => {
    const r = resolveIntegratedQueueDrain(['zanu 비교도 꼭 같이 봐줘', 'KOL용으로', '짧게'], {
      coveredIntents: ['todo_summary', 'iteration_plan']
    })

    expect(r.consumed).toEqual([])
    expect(r.unresolved).toEqual(['zanu 비교도 꼭 같이 봐줘', 'KOL용으로', '짧게'])
    expect(r.send).toBe(formatIntegratedBusyInput('zanu 비교도 꼭 같이 봐줘\n\nKOL용으로\n\n짧게'))
  })

  it('keeps a status query that sits next to a covered TODO duplicate', () => {
    const r = resolveIntegratedQueueDrain(['todo는?', '끝났어?', '짧게'], { coveredIntents: ['todo_summary'] })

    expect(r.consumed).toEqual(['todo는?'])
    expect(r.unresolved).toEqual(['끝났어?', '짧게'])
    expect(r.send).toBe(formatIntegratedBusyInput('끝났어?\n\n짧게'))
  })

  it('does not consume new steering that merely contains the verb phrase "to do" even when a TODO recap was covered', () => {
    // The real "todo는?" recap duplicate is consumed; "I want to do the zanu
    // comparison too" is genuinely-new steering (scenario 4 phrased in English),
    // so it must survive and be sent — never dropped on the bare 'to do' substring.
    const r = resolveIntegratedQueueDrain(['todo는?', 'I want to do the zanu comparison too', '짧게'], {
      coveredIntents: ['todo_summary']
    })

    expect(r.consumed).toEqual(['todo는?'])
    expect(r.unresolved).toEqual(['I want to do the zanu comparison too', '짧게'])
    expect(r.send).toBe(formatIntegratedBusyInput('I want to do the zanu comparison too\n\n짧게'))
    expect(r.remaining).toEqual([])
  })

  it('does not cross slash-command boundaries during resolution', () => {
    // The covered TODO before the boundary is consumed; the identical-looking
    // entry *after* the slash command is protected by the boundary and stays
    // queued verbatim alongside it.
    const r = resolveIntegratedQueueDrain(['todo는?', '/busy status', 'todo 또?'], {
      coveredIntents: ['todo_summary']
    })

    expect(r.consumed).toEqual(['todo는?'])
    expect(r.unresolved).toEqual([])
    expect(r.send).toBeUndefined()
    expect(r.remaining).toEqual(['/busy status', 'todo 또?'])
  })

  it('does not cross file/drop path boundaries during resolution', () => {
    const r = resolveIntegratedQueueDrain(['iterate 더 해줘', './notes.md', 'iterate again'], {
      coveredIntents: ['iteration_plan']
    })

    expect(r.consumed).toEqual(['iterate 더 해줘'])
    expect(r.send).toBeUndefined()
    // The post-boundary fragment is left untouched for the normal send path.
    expect(r.remaining).toEqual(['./notes.md', 'iterate again'])
  })

  it('treats slash commands and dropped-path entries as boundaries, never prose, even when covered', () => {
    for (const boundary of ['/busy status', '~/notes.md', './x.png', '"/Users/me/a"']) {
      const q = [boundary, 'todo는?']
      const r = resolveIntegratedQueueDrain(q, { coveredIntents: ['todo_summary', 'iteration_plan'] })

      // Nothing is filtered or coalesced — the queue is handed back unchanged
      // so the caller one-item dequeues the boundary entry.
      expect(r.consumed).toEqual([])
      expect(r.unresolved).toEqual([])
      expect(r.send).toBeUndefined()
      expect(r.remaining).toBe(q)
    }
  })

  it('drops a covered duplicate that precedes a slash command and leaves the command queued', () => {
    const r = resolveIntegratedQueueDrain(['todo는?', '/busy status'], { coveredIntents: ['todo_summary'] })

    expect(r.consumed).toEqual(['todo는?'])
    expect(r.send).toBeUndefined()
    expect(r.remaining).toEqual(['/busy status'])
  })
})

// --- Front-desk runtime wiring ---------------------------------------------
// `coveredIntentsFromSettledTurn` is the runtime input `useMainApp.ts` feeds to
// `resolveIntegratedQueueDrain` at busy→ready settle time: it derives a
// conservative `coveredIntents` from the transcript the TUI already holds.

const userMsg = (text: string): Msg => ({ role: 'user', text })
const assistantMsg = (text: string): Msg => ({ role: 'assistant', text })
const introMsg: Msg = { kind: 'intro', role: 'system', text: '' }
const todoTrailMsg = (todos: NonNullable<Msg['todos']>): Msg => ({ kind: 'trail', role: 'system', text: '', todos })
const toolTrailMsg = (text: string): Msg => ({ kind: 'trail', role: 'system', text })

describe('coveredIntentsFromSettledTurn', () => {
  it('reports todo_summary when the just-settled turn archived a TODO trail', () => {
    const history: Msg[] = [
      introMsg,
      userMsg('할 일 정리해서 작업해줘'),
      todoTrailMsg([
        { content: 'step 1', id: '1', status: 'completed' },
        { content: 'step 2', id: '2', status: 'in_progress' }
      ]),
      assistantMsg('진행 중입니다…')
    ]

    expect(coveredIntentsFromSettledTurn(history)).toEqual(['todo_summary'])
  })

  it('reports nothing when the just-settled turn tracked no TODOs', () => {
    const history: Msg[] = [introMsg, userMsg('zanu 비교 표 만들어줘'), toolTrailMsg('Read foo.ts'), assistantMsg('표 나갑니다…')]

    expect(coveredIntentsFromSettledTurn(history)).toEqual([])
  })

  it('ignores a TODO trail that belongs to an OLDER turn (scan stops at the most recent user message)', () => {
    const history: Msg[] = [
      introMsg,
      userMsg('첫 작업 해줘'),
      todoTrailMsg([{ content: 'old', id: 'old', status: 'completed' }]),
      assistantMsg('첫 작업 끝났습니다'),
      userMsg('이번엔 다른 거 해줘'), // ← the scan must stop here; the trail above is a prior turn's
      assistantMsg('다른 작업 진행 중…')
    ]

    expect(coveredIntentsFromSettledTurn(history)).toEqual([])
  })

  it('still finds the turn-end TODO trail when a mid-turn clarify answer sits before it', () => {
    const history: Msg[] = [
      introMsg,
      userMsg('할 일 잡고 진행해줘'),
      userMsg('네 그렇게 해주세요'), // clarify answer appended mid-turn — BEFORE the turn-end trail
      todoTrailMsg([{ content: 'x', id: 'x', status: 'pending' }]),
      assistantMsg('진행합니다…')
    ]

    expect(coveredIntentsFromSettledTurn(history)).toEqual(['todo_summary'])
  })

  it('reports nothing for an empty or intro-only transcript', () => {
    expect(coveredIntentsFromSettledTurn([])).toEqual([])
    expect(coveredIntentsFromSettledTurn([introMsg])).toEqual([])
  })

  it('ignores a trail message with an empty todos array', () => {
    const history: Msg[] = [introMsg, userMsg('go'), todoTrailMsg([]), assistantMsg('done')]

    expect(coveredIntentsFromSettledTurn(history)).toEqual([])
  })
})

describe('front-desk drain (coveredIntentsFromSettledTurn → resolveIntegratedQueueDrain)', () => {
  // Mirrors what useMainApp.ts does at settle time, minus the queueRef plumbing.
  const drainAtSettle = (queue: string[], history: Msg[]) =>
    resolveIntegratedQueueDrain(queue, { coveredIntents: coveredIntentsFromSettledTurn(history) })

  const todoTurn: Msg[] = [userMsg('할 일 잡고 작업해줘'), todoTrailMsg([{ content: 'a', id: 'a', status: 'completed' }]), assistantMsg('작업 진행 중…')]
  const plainTurn: Msg[] = [userMsg('zanu 비교 표'), assistantMsg('비교 진행 중…')]

  it('consumes a queued "todo는?" recap when the just-settled turn archived a TODO trail', () => {
    const r = drainAtSettle(['todo는?'], todoTurn)

    expect(r.consumed).toEqual(['todo는?'])
    expect(r.send).toBeUndefined()
    expect(r.remaining).toEqual([])
  })

  it('still replays a queued "todo는?" when the just-settled turn tracked no TODOs', () => {
    const r = drainAtSettle(['todo는?'], plainTurn)

    expect(r.consumed).toEqual([])
    expect(r.send).toBe(formatIntegratedBusyInput('todo는?'))
  })

  it('keeps a status query and brand-new steering even when a TODO trail was archived', () => {
    const r = drainAtSettle(['todo는?', '끝났어?', 'zanu 비교도 같이'], todoTurn)

    expect(r.consumed).toEqual(['todo는?'])
    expect(r.unresolved).toEqual(['끝났어?', 'zanu 비교도 같이'])
    expect(r.send).toBe(formatIntegratedBusyInput('끝났어?\n\nzanu 비교도 같이'))
  })

  it('never crosses a slash boundary even when a TODO trail was archived', () => {
    const q = ['/busy status', 'todo는?']
    const r = drainAtSettle(q, todoTurn)

    expect(r.consumed).toEqual([])
    expect(r.unresolved).toEqual([])
    expect(r.send).toBeUndefined()
    expect(r.remaining).toEqual(q)
  })

  it('treats a TODO recap queued after a later user turn as fresh, not covered by an older TODO trail', () => {
    const history: Msg[] = [
      ...todoTurn,
      userMsg('이제 zanu 비교를 해줘'),
      assistantMsg('비교 작업 중…')
    ]
    const r = drainAtSettle(['todo는?'], history)

    expect(r.consumed).toEqual([])
    expect(r.send).toBe(formatIntegratedBusyInput('todo는?'))
  })

  it('models staggered queueing across settle cycles instead of one simultaneous burst', () => {
    const firstDrain = drainAtSettle(['todo는?', 'zanu 비교도 같이 봐줘'], todoTurn)

    expect(firstDrain.consumed).toEqual(['todo는?'])
    expect(firstDrain.send).toBe(formatIntegratedBusyInput('zanu 비교도 같이 봐줘'))
    expect(firstDrain.remaining).toEqual([])

    // The user asks for TODO again later, during the follow-up turn that was
    // started by the first drain. The old TODO trail is now behind the most
    // recent user message, so it must not suppress this delayed queue item.
    const followupTurnWithoutTodos: Msg[] = [
      ...todoTurn,
      userMsg(firstDrain.send!),
      assistantMsg('zanu 비교 작업 중…')
    ]
    const delayedDrain = drainAtSettle(['todo는?'], followupTurnWithoutTodos)

    expect(delayedDrain.consumed).toEqual([])
    expect(delayedDrain.send).toBe(formatIntegratedBusyInput('todo는?'))
  })
})

// Hook coverage gap (deliberate): `useMainApp.ts` itself has no unit test in this
// repo, so the wiring there is covered by `tsc` plus these composition tests,
// which pin the exact `{ coveredIntents }` context the hook builds and passes to
// `resolveIntegratedQueueDrain`. A full hook test would need to drive a
// React/Ink render with a mock gateway and is left out of this pass.
