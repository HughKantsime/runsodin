import { describe, expect, it } from 'vitest'
import { formatMinutesAgo } from '../relativeTime'

describe('formatMinutesAgo', () => {
  it('reads just now under a minute and for bad input', () => {
    expect(formatMinutesAgo(0)).toBe('just now')
    expect(formatMinutesAgo(0.9)).toBe('just now')
    expect(formatMinutesAgo(NaN)).toBe('just now')
  })
  it('uses minutes under one hour', () => {
    expect(formatMinutesAgo(1)).toBe('1 minute ago')
    expect(formatMinutesAgo(59)).toBe('59 minutes ago')
  })
  it('uses hours from 60 minutes up to 48 hours', () => {
    expect(formatMinutesAgo(60)).toBe('1 hour ago')
    expect(formatMinutesAgo(491)).toBe('8 hours ago')
    expect(formatMinutesAgo(2879)).toBe('47 hours ago')
  })
  it('uses days from 48 hours', () => {
    expect(formatMinutesAgo(2880)).toBe('2 days ago')
    expect(formatMinutesAgo(5120)).toBe('3 days ago')
  })
})
