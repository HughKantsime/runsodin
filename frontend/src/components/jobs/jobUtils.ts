import type { JobStatus } from '../../types'

interface StatusOption {
  value: JobStatus | ''
  label: string
}

interface PriorityOption {
  value: number
  label: string
}

export const statusOptions: StatusOption[] = [
  { value: '', label: 'All Status' },
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

type QueueOrdered = { queue_position?: number | null; priority?: number | null; created_at?: string | null; id?: number }

// Mirrors GET /api/jobs and the scheduler: drag-and-drop queue_position first
// (unplaced jobs after), then priority (1 = highest), then age.
export function queueCompare(a: QueueOrdered, b: QueueOrdered): number {
  const pa = a.queue_position ?? null, pb = b.queue_position ?? null
  if (pa !== pb) {
    if (pa === null) return 1
    if (pb === null) return -1
    return pa - pb
  }
  const pr = (a.priority ?? 99) - (b.priority ?? 99)
  if (pr) return pr
  const ta = a.created_at ? Date.parse(a.created_at) : 0, tb = b.created_at ? Date.parse(b.created_at) : 0
  if (ta !== tb) return ta - tb
  return (a.id ?? 0) - (b.id ?? 0)
}
