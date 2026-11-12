import { describe, expect, it } from 'vitest'
import { fleetCounts, state } from '../fleetCounts'
import type { Printer } from '../../types'

const seen = () => new Date().toISOString()
const printer = (gcode_state: string, extra: Partial<Printer> = {}) =>
  ({ id: Math.random(), gcode_state, last_seen: seen(), is_active: true, ...extra }) as unknown as Printer

describe('fleetCounts', () => {
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
