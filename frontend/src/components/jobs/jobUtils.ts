import type { JobStatus } from '../../types'

interface StatusOption {
  value: JobStatus | '' | 'history' | 'all'
  label: string
}

interface PriorityOption {
  value: number
  label: string
}

export const statusOptions: StatusOption[] = [
  { value: '', label: 'Active' },
  { value: 'history', label: 'History' },
  { value: 'all', label: 'All Status' },
  { value: 'pending', label: 'Pending' },
  { value: 'scheduled', label: 'Scheduled' },
  { value: 'printing', label: 'Printing' },
  { value: 'paused', label: 'Paused' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
  { value: 'cancelled', label: 'Cancelled' },
]

export const priorityOptions: PriorityOption[] = [
  { value: 1, label: '1 - Highest' },
  { value: 2, label: '2 - High' },
  { value: 3, label: '3 - Normal' },
  { value: 4, label: '4 - Low' },
  { value: 5, label: '5 - Lowest' },
]

export const statusOrder: Record<string, number> = { printing: 0, paused: 1, scheduled: 2, pending: 3, failed: 4, cancelled: 5, completed: 6 }

// The queue: everything still waiting for, or on, a printer.
export const ACTIVE_STATUSES = ['submitted', 'rejected', 'pending', 'scheduled', 'printing', 'paused'] as const
export const HISTORY_STATUSES = ['completed', 'failed', 'cancelled'] as const

/** Statuses to request for a status-filter value (null = every status). '' and unknown values mean Active. */
export function statusesFor(filter: string): string[] | null {
  if (filter === 'all') return null
  if (filter === 'history') return [...HISTORY_STATUSES]
  if (!filter || !statusOptions.some(option => option.value === filter)) return [...ACTIVE_STATUSES]
  return [filter]
}

/** Queue order for the active views; newest end first for anything that includes finished jobs. */
export function orderFor(filter: string): 'queue' | 'recent' {
  const statuses = statusesFor(filter)
  return statuses && statuses.every(status => (ACTIVE_STATUSES as readonly string[]).includes(status)) ? 'queue' : 'recent'
}

type QueueOrdered = { queue_position?: number | null; priority?: number | null; created_at?: string | null; id?: number }

// Mirrors GET /api/jobs, the scheduler and dispatch: priority (1 = highest)
// first, then the drag-and-drop queue_position within a priority (unplaced
// jobs after placed ones), then age, then id.
export function queueCompare(a: QueueOrdered, b: QueueOrdered): number {
  const pr = (a.priority ?? 99) - (b.priority ?? 99)
  if (pr) return pr
  const pa = a.queue_position ?? null, pb = b.queue_position ?? null
  if (pa !== pb) {
    if (pa === null) return 1
    if (pb === null) return -1
    return pa - pb
  }
  const ta = a.created_at ? Date.parse(a.created_at) : 0, tb = b.created_at ? Date.parse(b.created_at) : 0
  if (ta !== tb) return ta - tb
  return (a.id ?? 0) - (b.id ?? 0)
}
