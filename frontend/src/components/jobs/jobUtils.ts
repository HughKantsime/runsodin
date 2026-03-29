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
