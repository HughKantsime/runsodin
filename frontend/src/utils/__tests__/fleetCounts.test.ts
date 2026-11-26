import { describe, expect, it } from 'vitest'
import { fleetCounts, needsYou, state } from '../fleetCounts'
import type { Printer } from '../../types'

const seen = () => new Date().toISOString()
const printer = (gcode_state: string, extra: Partial<Printer> = {}) =>
  ({ id: Math.random(), gcode_state, last_seen: seen(), is_active: true, ...extra }) as unknown as Printer

describe('fleetCounts', () => {
  it.each([false, true, undefined])('classifies a failed printer with attention %s', flag => {
    const p = printer('FAILED', flag === undefined ? {} : { state_needs_attention: flag })
    const ready = flag === false
    expect(needsYou(p)).toBe(!ready)
    expect(state(p)).toBe(ready ? 'ready' : 'needs you')
    expect(fleetCounts([p])).toEqual({ printing: 0, ready: ready ? 1 : 0, attention: ready ? 0 : 1, offline: 0, total: 1 })
  })

  it('still shows pauses and HMS faults, and ignores informational HMS notices', () => {
    const quiet = { state_needs_attention: false }
    const paused = printer('PAUSED', quiet)
    const fault = printer('FAILED', { ...quiet, hms_errors: '[{"severity":"error"}]' })
    const notice = printer('FAILED', { ...quiet, hms_errors: '[{"severity":"info"}]' })
    expect([paused, fault, notice].map(needsYou)).toEqual([true, true, false])
    expect([paused, fault, notice].map(state)).toEqual(['needs you', 'needs you', 'ready'])
    expect(fleetCounts([paused, fault, notice])).toEqual({ printing: 0, ready: 1, attention: 2, offline: 0, total: 3 })
  })

  it('never counts a paused printer as printing, for either protocol vocabulary', () => {
    const c = fleetCounts([printer('RUNNING'), printer('PRINTING'), printer('PAUSE'), printer('PAUSED')])
    expect(c.printing).toBe(2)
    expect(c.attention).toBe(2)
  })

  it('counts ready, offline and needs-you printers once each', () => {
    const c = fleetCounts([
      printer('IDLE'), printer('FINISH'), printer('ERROR'),
      printer('RUNNING', { last_seen: '2020-01-01T00:00:00Z' }),
    ])
    expect(c).toEqual({ printing: 0, ready: 2, attention: 1, offline: 1, total: 4 })
  })

  it('agrees with the per-printer state used by the badges', () => {
    const fleet = [printer('RUNNING'), printer('PAUSE'), printer('IDLE')]
    const c = fleetCounts(fleet)
    expect(fleet.filter(p => state(p) === 'printing')).toHaveLength(c.printing)
  })
})
