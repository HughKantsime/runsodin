import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { isOnline } from './shared'
import type { Printer } from '../types'
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-11T12:00:00Z')) })
afterEach(() => vi.useRealTimers())
const printer = (last_seen: string | null) => ({ last_seen } as Printer)
it('accepts a recent timezone-less UTC heartbeat', () => expect(isOnline(printer('2026-10-11T11:59:30'))).toBe(true))
it('accepts a recent explicitly UTC heartbeat', () => expect(isOnline(printer('2026-10-11T11:59:30Z'))).toBe(true))
it('accepts a recent offset-qualified heartbeat', () => expect(isOnline(printer('2026-10-11T07:59:30-04:00'))).toBe(true))
it('expires at the exact 90 second threshold', () => expect(isOnline(printer('2026-10-11T11:58:30'))).toBe(false))
it('does not claim a future heartbeat proves online status', () => expect(isOnline(printer('2026-10-11T12:01:00'))).toBe(false))
it('treats missing and malformed heartbeats as offline', () => {
 expect(isOnline(printer(null))).toBe(false)
 expect(isOnline(printer('invalid'))).toBe(false)
})
it('accepts fractional seconds from database serialization', () => expect(isOnline(printer('2026-10-11T11:59:30.123456'))).toBe(true))
it('accepts a positive offset without replacing its timezone', () => expect(isOnline(printer('2026-10-11T17:29:30+05:30'))).toBe(true))
it('accepts a UTC timestamp surrounded by whitespace', () => expect(isOnline(printer(' 2026-10-11T11:59:30Z '))).toBe(true))
it('honors an explicitly offline monitor state even with a recent heartbeat', () => {
 expect(isOnline({ ...printer('2026-10-11T11:59:59Z'), gcode_state: 'OFFLINE' } as Printer)).toBe(false)
})
