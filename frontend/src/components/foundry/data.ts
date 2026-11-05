import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { printers, printJobs, alerts } from '../../api'
import { fetchAPI } from '../../api/client'
import { canAccessPage } from '../../permissions'
import { isOnline, isPausedState, isPrintingState } from '../../utils/shared'
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
export function needsYou(printer: Printer) {
  // HMS is a JSON string on HTTP and an array on websocket updates. '[]' is not an error.
  let errors: unknown = printer.hms_errors
  if (typeof errors === 'string') { try { errors = JSON.parse(errors) } catch { errors = [] } }
  return isPausedState(printer.gcode_state) || ['ERROR', 'FAILED', 'RUNOUT'].includes((printer.gcode_state || '').toUpperCase())
    || (Array.isArray(errors) && errors.length > 0)
}
export function state(printer: Printer) {
  if (!printer.gcode_state || printer.gcode_state.trim().toUpperCase() === 'UNKNOWN') return 'unknown'
  if (!isOnline(printer)) return 'offline'
  if (needsYou(printer)) return 'needs you'
  if (isPrintingState(printer.gcode_state)) return 'printing'
  return printer.gcode_state || 'unknown'
}
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
  const notices = useQuery({ queryKey: ['alerts', 'foundry'], queryFn: async () => {
    // Paginate: a large farm must not lose its paused printer behind the default alert limit.
    const result = []
    for (let offset = 0; ; offset += 100) {
      const page = await alerts.list({ limit: 100, offset })
      result.push(...page)
      if (page.length < 100) return result
    }
  }, enabled: canAccessPage('alerts'), refetchInterval: 30000 })
  return { fleet, prints, queue, notices }
}
