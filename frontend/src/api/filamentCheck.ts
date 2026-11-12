import { fetchAPI } from './client'

export type FilamentSlotStatus = 'enough' | 'short' | 'unknown'

export interface FilamentCheckSlot {
  slot: number | null
  source_slots: number[]
  material: string | null
  color: string | null
  status: FilamentSlotStatus
  needed_g: number | null
  print_g: number | null
  remaining_g: number | null
  spool_id: number | null
  reason: string | null
  message: string
}

export interface FilamentCheck {
  job_id: number
  printer_id: number
  mode: 'off' | 'warn' | 'hold'
  margin_pct: number
  prints: number | null
  overall: 'enough' | 'short' | 'unknown' | 'off'
  would_hold: boolean
  slots: FilamentCheckSlot[]
  warnings: string[]
}

export const filamentCheck = {
  get: (jobId: number, printerId: number) =>
    fetchAPI<FilamentCheck>(`/jobs/${jobId}/filament-check?printer_id=${printerId}`),
}
