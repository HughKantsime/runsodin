import { describe, expect, test } from 'vitest'
import { groupAlerts, type GroupableAlert } from './alertGroups'

const H = 3600 * 1000
const BASE = Date.parse('2026-10-11T12:00:00Z')
let nextId = 1
function mk(hoursAgo: number, over: Partial<GroupableAlert> = {}): GroupableAlert {
  return {
    id: nextId++,
    alert_type: 'spaghetti_detected',
    severity: 'critical',
    title: 'Spaghetti on P1',
    printer_id: 1,
    is_read: false,
    created_at: new Date(BASE - hoursAgo * H).toISOString(),
    ...over,
  }
}

describe('groupAlerts', () => {
  test('same type, severity, printer and title group; title is trimmed and case-insensitive', () => {
    const g = groupAlerts([mk(0), mk(1, { title: '  SPAGHETTI on p1 ' }), mk(2)])
    expect(g).toHaveLength(1)
    expect(g[0].alerts).toHaveLength(3)
  })

  test.each([
    ['printer', { printer_id: 2 }],
    ['type', { alert_type: 'print_failed' }],
    ['severity', { severity: 'warning' }],
    ['title', { title: 'Something else' }],
  ])('different %s does not group', (_n, over) => {
    expect(groupAlerts([mk(0), mk(1, over as Partial<GroupableAlert>)])).toHaveLength(2)
  })

  test('a gap over 12 hours splits; exactly 12 hours stays together', () => {
    expect(groupAlerts([mk(0), mk(12)])).toHaveLength(1)
    expect(groupAlerts([mk(0), mk(12.01)])).toHaveLength(2)
  })

  test('gap is measured to the previous member, so chains longer than 12 hours stay grouped', () => {
    const g = groupAlerts([mk(0), mk(10), mk(20), mk(30)])
    expect(g).toHaveLength(1)
    expect(g[0].alerts).toHaveLength(4)
  })

  test('order is preserved newest first, groups sit at their newest member', () => {
    const a1 = mk(0)
    const b1 = mk(1, { printer_id: 2 })
    const a2 = mk(2)
    const c1 = mk(3, { title: 'Other' })
    const g = groupAlerts([a1, b1, a2, c1])
    expect(g.map(x => x.alerts.map(a => a.id))).toEqual([[a1.id, a2.id], [b1.id], [c1.id]])
    expect(g[0].newest).toBe(a1)
    expect(g[0].oldest).toBe(a2)
  })

  test('a lone alert is a group of one', () => {
    const only = mk(0)
    const g = groupAlerts([only])
    expect(g).toHaveLength(1)
    expect(g[0].alerts).toEqual([only])
    expect(g[0].newest).toBe(only)
    expect(g[0].oldest).toBe(only)
  })

  test('unread state is any member unread', () => {
    expect(groupAlerts([mk(0, { is_read: true }), mk(1, { is_read: false })])[0].anyUnread).toBe(true)
    expect(groupAlerts([mk(0, { is_read: true }), mk(1, { is_read: true })])[0].anyUnread).toBe(false)
  })

  test('null printer_id groups only with null', () => {
    const g = groupAlerts([mk(0, { printer_id: null }), mk(1, { printer_id: null }), mk(2, { printer_id: 0 }), mk(3, { printer_id: 1 })])
    expect(g.map(x => x.alerts.length)).toEqual([2, 1, 1])
  })

  test('empty input and unparsable timestamps', () => {
    expect(groupAlerts([])).toEqual([])
    expect(groupAlerts([mk(0, { created_at: 'nope' }), mk(1, { created_at: 'nope' })])).toHaveLength(2)
  })
})
