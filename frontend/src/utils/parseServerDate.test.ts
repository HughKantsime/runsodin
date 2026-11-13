import { describe, expect, it } from 'vitest'
import { formatDate, parseServerDate } from './shared'

describe('parseServerDate', () => {
  it('reads a zone-less server timestamp as UTC, not local time', () => {
    expect(parseServerDate('2026-11-13T03:18:20').toISOString()).toBe('2026-11-13T03:18:20.000Z')
    expect(parseServerDate('2026-11-13 03:18:20').toISOString()).toBe('2026-11-13T03:18:20.000Z')
    expect(parseServerDate('2026-11-13T03:18:20.123456').getTime()).toBe(Date.UTC(2026, 10, 13, 3, 18, 20, 123))
  })

  it('leaves a value that carries its own zone alone', () => {
    expect(parseServerDate('2026-11-13T03:18:20Z').toISOString()).toBe('2026-11-13T03:18:20.000Z')
    expect(parseServerDate('2026-11-12T22:18:20-05:00').toISOString()).toBe('2026-11-13T03:18:20.000Z')
    expect(parseServerDate('2026-11-13T03:18:20+0000').toISOString()).toBe('2026-11-13T03:18:20.000Z')
  })

  it('keeps a date without a time on that calendar day in the viewer\'s zone', () => {
    const day = parseServerDate('2026-11-13')
    expect([day.getFullYear(), day.getMonth(), day.getDate(), day.getHours()]).toEqual([2026, 10, 13, 0])
  })

  it('passes dates and numbers through and never throws on junk', () => {
    const now = new Date()
    expect(parseServerDate(now)).toBe(now)
    expect(parseServerDate(1794539900000).getTime()).toBe(1794539900000)
    expect(Number.isNaN(parseServerDate(null).getTime())).toBe(true)
    expect(Number.isNaN(parseServerDate('not a date').getTime())).toBe(true)
  })

  it('formatDate shows the same instant the server meant', () => {
    const shown = formatDate('2026-11-13T03:18:20')
    const expected = new Date(Date.UTC(2026, 10, 13, 3, 18, 20)).toLocaleString([], { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })
    expect(shown).toBe(expected)
  })
})
