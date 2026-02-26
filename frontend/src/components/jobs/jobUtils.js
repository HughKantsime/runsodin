export function formatHours(h) {
  if (!h) return "—"
  if (h < 1) return Math.round(h * 60) + "m"
  const hrs = Math.floor(h)
  const mins = Math.round((h - hrs) * 60)
  return mins > 0 ? hrs + "h " + mins + "m" : hrs + "h"
}

export const statusOptions = [
  { value: '', label: 'All Status' },
  { value: 'submitted', label: 'Submitted' },
  { value: 'pending', label: 'Pending' },
  { value: 'scheduled', label: 'Scheduled' },
  { value: 'printing', label: 'Printing' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
  { value: 'rejected', label: 'Rejected' },
]

export const priorityOptions = [
  { value: 1, label: '1 - Highest' },
  { value: 2, label: '2 - High' },
  { value: 3, label: '3 - Normal' },
  { value: 4, label: '4 - Low' },
  { value: 5, label: '5 - Lowest' },
]

export const statusOrder = { submitted: 0, printing: 1, scheduled: 2, pending: 3, rejected: 4, failed: 5, completed: 6 }
