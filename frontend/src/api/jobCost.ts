import { fetchAPI } from './client'

export type CostUnknownReason =
  | 'usage_not_recorded' | 'usage_unmeasured' | 'spool_price_missing' | 'spool_weight_missing' | 'price_not_recorded'
  | 'print_time_not_recorded' | 'attempt_not_finished' | 'settings_incomplete'

export interface CostUnknown {
  part: 'material' | 'machine' | 'attempt' | 'settings'
  reason: CostUnknownReason
  attempt_id: number | null
  spool_id: number | null
}

export interface CostSpoolLine {
  spool_id: number | null
  name?: string | null
  grams: number
  amount: number | null
  provenance: 'measured' | 'sliced_estimate' | 'mixed' | null
}

export interface CostAttempt {
  id: number
  outcome: 'completed' | 'failed' | 'cancelled' | 'open'
  hours: number | null
  material: number
  machine: number | null
  amount: number
  complete: boolean
}

// `configured` false (no saved pricing settings) or `finished` false: the server sends no amounts.
// state 'none': no print attempt was recorded. state 'partial': `total` is a lower bound.
export interface JobCost {
  job_id: number
  configured: boolean
  finished: boolean
  state?: 'none' | 'complete' | 'partial'
  estimate?: number | null
  difference?: number | null
  pieces?: number | null
  total?: number
  unknown?: CostUnknown[]
  material?: { amount: number; complete: boolean; spools: CostSpoolLine[] }
  machine?: { amount: number; complete: boolean; hours: number; rate_per_hour: number | null; basis: 'current_rates' }
  from_settings?: { amount: number | null; complete: boolean; basis: 'settings'; labor: number | null; packaging: number | null; other: number | null; overhead: number | null }
  failed_attempts?: { amount: number; complete: boolean; count: number }
  attempts?: CostAttempt[]
}

export const jobCost = {
  get: (jobId: number) => fetchAPI<JobCost>(`/jobs/${jobId}/cost`),
}
