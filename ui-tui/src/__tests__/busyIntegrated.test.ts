import { describe, expect, it } from 'vitest'

import {
  coalesceIntegratedQueue,
  formatIntegratedBusyInput,
  INTEGRATED_BUSY_PREAMBLE
} from '../app/busyIntegrated.js'

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
