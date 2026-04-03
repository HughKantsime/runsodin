import { describe, it, expect, vi, afterEach } from 'vitest'
import {
  formatDurationSecs,
  formatDate,
  isOnline,
  ONLINE_THRESHOLD_MS,
} from '../shared'

describe('formatDurationSecs', () => {
  it('returns "--" for falsy values', () => {
    expect(formatDurationSecs(0)).toBe('--')
    expect(formatDurationSecs(null)).toBe('--')
    expect(formatDurationSecs(undefined)).toBe('--')
  })

  it('formats seconds less than an hour as minutes', () => {
    expect(formatDurationSecs(120)).toBe('2m')
    expect(formatDurationSecs(300)).toBe('5m')
    expect(formatDurationSecs(59)).toBe('0m')
  })

  it('formats seconds greater than an hour as hours and minutes', () => {
    expect(formatDurationSecs(3600)).toBe('1h 0m')
    expect(formatDurationSecs(3661)).toBe('1h 1m')
    expect(formatDurationSecs(7200)).toBe('2h 0m')
    expect(formatDurationSecs(5400)).toBe('1h 30m')
  })
})

describe('formatDate', () => {
  it('returns "--" for falsy values', () => {
    expect(formatDate(null)).toBe('--')
    expect(formatDate(undefined)).toBe('--')
    expect(formatDate('')).toBe('--')
  })

  it('formats a valid ISO date string', () => {
    const result = formatDate('2026-01-05T15:45:00Z')
    // The output depends on locale, but should contain the date components
    expect(result).not.toBe('--')
    expect(typeof result).toBe('string')
    expect(result.length).toBeGreaterThan(0)
  })

  it('includes month, day, and year in output', () => {
    const result = formatDate('2026-06-15T10:30:00Z')
    // Should contain "Jun" (short month), "15" (day), "2026" (year)
    expect(result).toContain('Jun')
    expect(result).toContain('15')
    expect(result).toContain('2026')
  })
})

describe('isOnline', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('returns false when last_seen is null or undefined', () => {
    expect(isOnline({ last_seen: null })).toBeFalsy()
    expect(isOnline({ last_seen: undefined })).toBeFalsy()
    expect(isOnline({})).toBeFalsy()
  })

  it('returns true when printer was seen recently', () => {
    // Set Date.now to a fixed time
    const now = new Date('2026-03-03T12:00:00Z').getTime()
    vi.spyOn(Date, 'now').mockReturnValue(now)

    // last_seen 30 seconds ago (well within the 90-second threshold)
    const printer = { last_seen: '2026-03-03T11:59:30' }
    expect(isOnline(printer)).toBe(true)
  })

  it('returns false when printer was seen too long ago', () => {
    const now = new Date('2026-03-03T12:00:00Z').getTime()
    vi.spyOn(Date, 'now').mockReturnValue(now)

    // last_seen 2 minutes ago (beyond the 90-second threshold)
    const printer = { last_seen: '2026-03-03T11:58:00' }
    expect(isOnline(printer)).toBe(false)
  })

  it('uses the ONLINE_THRESHOLD_MS constant (90 seconds)', () => {
    expect(ONLINE_THRESHOLD_MS).toBe(90_000)
  })
})
