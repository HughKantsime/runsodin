import { afterEach, describe, expect, it } from 'vitest'
import { formatServerDay, parseServerDate, parseServerDay, serverDayOffset } from './shared'

// A due date picked as "Nov 20" is stored as midnight UTC. The same stored value must be
// Nov 20 for a viewer in any zone: west of UTC, on UTC, and far east of it.
const ZONES = ['America/Los_Angeles', 'UTC', 'Pacific/Auckland']
const STORED = ['2026-11-20T00:00:00', '2026-11-20T00:00:00Z', '2026-11-20T00:00:00+00:00', '2026-11-20 00:00:00', '2026-11-20']
const originalZone = process.env.TZ
afterEach(() => { if (originalZone === undefined) delete process.env.TZ; else process.env.TZ = originalZone })

describe.each(ZONES)('calendar days for a viewer in %s', zone => {
  it.each(STORED)('shows %s as November 20', stored => {
    process.env.TZ = zone
    const day = parseServerDay(stored)!
    expect([day.getFullYear(), day.getMonth(), day.getDate(), day.getHours()]).toEqual([2026, 10, 20, 0])
    expect(formatServerDay(stored, undefined, 'en-US')).toBe('Nov 20, 2026')
    expect(formatServerDay(stored, { weekday: 'long', month: 'long', day: 'numeric' }, 'en-US')).toBe('Friday, November 20')
  })

  it('counts whole days from the viewer\'s own today', () => {
    process.env.TZ = zone
    const stored = '2026-11-20T00:00:00'
    expect(serverDayOffset(stored, new Date(2026, 10, 20, 0, 0, 1))).toBe(0)
    expect(serverDayOffset(stored, new Date(2026, 10, 20, 23, 59, 59))).toBe(0)
    expect(serverDayOffset(stored, new Date(2026, 10, 19, 23, 59, 59))).toBe(1)
    expect(serverDayOffset(stored, new Date(2026, 10, 21, 0, 0, 1))).toBe(-1)
    expect(serverDayOffset(stored, new Date(2026, 10, 10, 12))).toBe(10)
    // across both daylight-saving changes of either hemisphere a day is still one day
    expect(serverDayOffset('2026-03-09T00:00:00', new Date(2026, 2, 7, 12))).toBe(2)
    expect(serverDayOffset('2026-09-28T00:00:00', new Date(2026, 8, 26, 12))).toBe(2)
    expect(serverDayOffset('2027-01-01T00:00:00', new Date(2026, 11, 31, 22))).toBe(1)
  })
})

describe('calendar day helpers', () => {
  it('differ from the instant reading that showed the day before, west of UTC', () => {
    process.env.TZ = 'America/Los_Angeles'
    expect(parseServerDate('2026-11-20T00:00:00').getDate()).toBe(19)
    expect(parseServerDay('2026-11-20T00:00:00')!.getDate()).toBe(20)
  })

  it('return nothing for a missing or impossible day and never throw', () => {
    for (const junk of [null, undefined, '', 'soon', '2026-02-31T00:00:00', '2026-13-01', '20261120', '2026-11-2']) {
      expect(parseServerDay(junk)).toBeNull()
      expect(serverDayOffset(junk)).toBeNull()
      expect(formatServerDay(junk)).toBe('--')
    }
  })
})
