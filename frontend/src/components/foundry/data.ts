import { needsYou, state, fleetCounts } from '../../utils/fleetCounts'
import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { printers, printJobs, alerts } from '../../api'
import { fetchAPI } from '../../api/client'
import { canAccessPage } from '../../permissions'
export { needsYou, state, fleetCounts }
import type { Printer, PrintJob, Job } from '../../types'

export const number = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
export function utc(value: unknown): number {
  if (typeof value !== 'string' || !value) return NaN
  return Date.parse(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`)
}
export function useNow() {
  const [now, setNow] = useState(Date.now)
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 30000); return () => clearInterval(timer) }, [])
  return now
}
// needsYou, state and fleetCounts live in utils/fleetCounts (shared with the sidebar summary).
export function activePrint(printer: Printer, prints: PrintJob[]) {
  return prints.filter(job => job.printer_id === printer.id && job.status === 'running').sort((a, b) => b.id - a.id)[0]
}
export function nextJob(printer: Printer, queue: Job[]) {
  return queue.filter(job => (job.printer_id ?? job.pinned_printer_id) === printer.id && ['scheduled', 'pending'].includes(job.status))
    .sort((a, b) => (utc(a.scheduled_start) || Infinity) - (utc(b.scheduled_start) || Infinity) || (a.queue_position ?? Infinity) - (b.queue_position ?? Infinity) || a.id - b.id)[0]
}
export function useFleet() {
  return useQuery({ queryKey: ['printers'], queryFn: () => printers.list(true), refetchInterval: 30000 })
}
export async function readQueue(): Promise<Job[]> {
  const result: Job[] = []
  for (let offset = 0; ; offset += 100) {
    const page = await fetchAPI<Job[]>(`/jobs?limit=100&offset=${offset}`)
    result.push(...page)
    if (page.length < 100) return result
  }
}
export function useFloorData() {
  const fleet = useFleet()
  const prints = useQuery({ queryKey: ['print-jobs', 'foundry-running'], queryFn: () => printJobs.list({ status: 'running', limit: 1000 }), refetchInterval: 30000 })
  const queue = useQuery({ queryKey: ['jobs', 'foundry'], queryFn: readQueue, enabled: canAccessPage('jobs') || canAccessPage('timeline'), refetchInterval: 30000 })
  // The server returns one pause/error per printer in a single request. A noisy
  // printer's history cannot crowd another paused printer out of the response.
  const notices = useQuery({ queryKey: ['alerts', 'foundry'], queryFn: () => alerts.list({ needs_you: true }), enabled: canAccessPage('alerts'), refetchInterval: 30000 })
  return { fleet, prints, queue, notices }
}
