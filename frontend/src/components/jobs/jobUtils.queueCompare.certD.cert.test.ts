// cert-D F04-4.3-01: client queue order matches the producer order (priority, queue_position NULLs last, age, id).
import { expect, it } from 'vitest'
import { queueCompare } from './jobUtils'

it('orders by priority first, then queue position within a priority with unplaced jobs last, then age', () => {
  const jobs = [
    { id: 1, priority: 3, queue_position: null, created_at: '2026-10-01T00:00:00' },
    { id: 2, priority: 3, queue_position: 1, created_at: '2026-10-03T00:00:00' },
    { id: 3, priority: 1, queue_position: null, created_at: '2026-10-05T00:00:00' },
    { id: 4, priority: 3, queue_position: 0, created_at: '2026-10-04T00:00:00' },
    { id: 5, priority: 3, queue_position: null, created_at: '2026-09-30T00:00:00' },
    { id: 6, priority: 1, queue_position: 0, created_at: '2026-10-06T00:00:00' },
  ]
  expect([...jobs].sort(queueCompare).map(j => j.id)).toEqual([6, 3, 4, 2, 5, 1])
})
