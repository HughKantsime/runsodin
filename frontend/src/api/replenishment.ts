import { fetchAPI } from './client'

export interface PrintNextRecipe { id: string; name: string; pieces_per_plate: number; printer_id: number; materials: string; hours_per_plate: number | null; hours_per_piece: number | null }
export interface PrintNextRow {
  product_id: number
  product_name: string
  reorder_point: number
  target: number
  minimum_batch: number
  status: 'ok' | 'below' | 'blocked'
  reason: string
  below_point: boolean | null
  on_hand: { quantity: number; read_at: string; age_seconds: number; source: 'square' | 'snapshot' } | null
  shortfall: number | null
  plates_needed: number
  recipe: PrintNextRecipe | null
  rank: number | null
  rank_reason: string | null
  on_the_way: { pieces: number; job_ids: number[]; awaiting_stock: number }
  open_orders: { pieces: number; oldest: string | null } | null
  velocity: { units_per_week: number; source: string; weeks: number } | null
}
export interface PrintNextList {
  generated_at: string
  stock: { connected: boolean; read_at: string | null; age_seconds: number | null; source: 'square' | 'snapshot' | 'none'; error: string | null; refresh_in_seconds: number; cache_seconds: number }
  rows: PrintNextRow[]
  truncated: boolean
  print_next_count: number
}
export interface QueueItem { product_id: number; recipe_id: string; plates: number }
export interface QueueResult {
  queued: { product_id: number; recipe_id: string; plates: number; job_ids: number[]; held: boolean }[]
  skipped: { product_id: number; reason: string }[]
}

export const replenishment = {
  /** `cached` answers from the last read or recorded snapshot and never contacts Square. */
  list: (cached = false, quiet = false) => fetchAPI<PrintNextList>(`/production/replenishment${cached ? '?cached=true' : ''}`, { quiet }),
  queue: (items: QueueItem[], ready: boolean) => fetchAPI<QueueResult>('/production/replenishment/queue', { method: 'POST', body: JSON.stringify({ items, ready }) }),
}
