import { describe, expect, it } from 'vitest'
import { ACTIVE_STATUSES, orderFor, statusesFor, statusOptions } from './jobUtils'

describe('status filter to server request', () => {
  it('opens on Active: submitted, pending, scheduled, printing, paused, in queue order', () => {
    expect(statusOptions[0]).toEqual({ value: '', label: 'Active' })
    expect(statusesFor('')).toEqual(['submitted', 'rejected', 'pending', 'scheduled', 'printing', 'paused'])
    expect(statusesFor('')).toEqual([...ACTIVE_STATUSES])
    expect(orderFor('')).toBe('queue')
  })

  it('History is completed, failed, cancelled and newest first', () => {
    expect(statusesFor('history')).toEqual(['completed', 'failed', 'cancelled'])
    expect(orderFor('history')).toBe('recent')
  })

  it('All Status sends no status filter and is newest first', () => {
    expect(statusesFor('all')).toBeNull()
    expect(orderFor('all')).toBe('recent')
  })

  it('a single status passes through; a finished one is newest first, an active one in queue order', () => {
    expect(statusesFor('pending')).toEqual(['pending'])
    expect(orderFor('pending')).toBe('queue')
    expect(statusesFor('completed')).toEqual(['completed'])
    expect(orderFor('completed')).toBe('recent')
  })

  it('an unknown value in an old link falls back to Active instead of a 422', () => {
    expect(statusesFor('bogus')).toEqual([...ACTIVE_STATUSES])
  })
})
