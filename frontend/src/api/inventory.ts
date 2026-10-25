import { fetchAPI } from './client'
import type {
  Spool,
  PurchaseEvidence,
  SpoolCreate,
  SpoolListFilters,
  SpoolDryingRecord,
  Filament,
  FilamentCreate,
  Consumable,
  ConsumableCreate,
  ConsumableUpdate,
  ConsumableAdjust,
} from '../types'

// QR code spool scanning + full CRUD
export interface SpoolmanLinkConflict { spoolman_spool_id: number; spools: { id: number | null; qr_code: string | null; org_id: number | null; label: string }[] }

export interface ReorderRow { material: string; color: string; sealed_rolls: number; sealed_g: number; open_g: number; total_g: number; threshold_g: number; reorder: boolean; shortfall_g: number }
export interface ReceiveRolls { filament_id: number; quantity: number; initial_weight_g: number; storage_location: string; sealed: boolean }
export const spools = {
  reorder: (orgId?: number | null): Promise<ReorderRow[]> => fetchAPI('/spools/reorder' + (orgId != null ? `?org_id=${orgId}` : '')),
  setReorderPolicy: (data: { material: string; color: string; threshold_g: number }): Promise<unknown> => fetchAPI('/spools/reorder-policy', { method: 'PUT', body: JSON.stringify(data) }),
  receive: (data: ReceiveRolls): Promise<{ spools: { id: number; qr_code: string }[] }> => fetchAPI('/spools/receive', { method: 'POST', body: JSON.stringify(data) }),
  /** Admin-only: Spoolman spools linked to more than one ODIN spool (403 for other roles). */
  linkConflicts: (): Promise<SpoolmanLinkConflict[]> => fetchAPI('/spools/spoolman-link-conflicts'),
  list: (filters: SpoolListFilters = {}): Promise<Spool[]> => {
    const params = new URLSearchParams()
    if (filters.status) params.append('status', filters.status)
    if (filters.printer_id) params.append('printer_id', String(filters.printer_id))
    if (filters.org_id != null) params.append('org_id', String(filters.org_id))
    return fetchAPI(`/spools?${params}`)
  },
  purchaseEvidence: (id: number): Promise<PurchaseEvidence> => fetchAPI(`/spools/${id}/purchase-evidence`),
  get: (id: number): Promise<Spool> => fetchAPI(`/spools/${id}`),
  create: (data: SpoolCreate): Promise<Spool> => fetchAPI('/spools', { method: 'POST', body: JSON.stringify(data) }),
  update: ({ id, ...data }: { id: number } & Record<string, unknown>): Promise<Spool> => fetchAPI(`/spools/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  load: ({ id, printer_id, slot_number }: { id: number; printer_id: number; slot_number: number }): Promise<Spool> => fetchAPI(`/spools/${id}/load`, {
    method: 'POST', body: JSON.stringify({ printer_id, slot_number }),
  }),
  unload: ({ id, storage_location }: { id: number; storage_location?: string }): Promise<Spool> => fetchAPI(`/spools/${id}/unload?storage_location=${storage_location || ''}`, { method: 'POST' }),
  use: ({ id, weight_used_g, notes }: { id: number; weight_used_g: number; notes?: string }): Promise<Spool> => fetchAPI(`/spools/${id}/use`, {
    method: 'POST', body: JSON.stringify({ weight_used_g, notes }),
  }),
  archive: (id: number): Promise<void> => fetchAPI(`/spools/${id}`, { method: 'DELETE' }),
  lookup: (qrCode: string): Promise<Spool> => fetchAPI(`/spools/lookup/${qrCode}`),
  scanAssign: (qrCode: string, printerId: number, slot: number): Promise<unknown> => fetchAPI('/spools/scan-assign', {
    method: 'POST',
    body: JSON.stringify({ qr_code: qrCode, printer_id: printerId, slot: slot }),
  }),
  logDrying: (spoolId: number, data: { duration_hours: number; method?: string; temp_c?: number; notes?: string }): Promise<unknown> => {
    const params = new URLSearchParams({ duration_hours: String(data.duration_hours), method: data.method || 'dryer' })
    if (data.temp_c) params.set('temp_c', String(data.temp_c))
    if (data.notes) params.set('notes', data.notes)
    return fetchAPI(`/spools/${spoolId}/dry?${params}`, { method: 'POST' })
  },
  dryingHistory: (spoolId: number): Promise<SpoolDryingRecord[]> => fetchAPI(`/spools/${spoolId}/drying-history`),
};

export const filaments = {
  list: (): Promise<Filament[]> => fetchAPI('/filaments'),
  combined: (): Promise<Filament[]> => fetchAPI('/filaments/combined'),
  add: (data: FilamentCreate): Promise<Filament> => fetchAPI('/filaments', { method: 'POST', body: JSON.stringify(data) }),
  create: (data: FilamentCreate): Promise<Filament> => fetchAPI('/filaments', { method: 'POST', body: JSON.stringify(data) }),
  update: ({ id, ...data }: { id: number } & Record<string, unknown>): Promise<Filament> => fetchAPI(`/filaments/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  remove: (id: number): Promise<void> => fetchAPI(`/filaments/${id}`, { method: 'DELETE' }),
}

// Consumables
export const consumables = {
  list: (status?: string): Promise<Consumable[]> => fetchAPI('/consumables' + (status ? '?status=' + status : '')),
  get: (id: number): Promise<Consumable> => fetchAPI(`/consumables/${id}`),
  create: (data: ConsumableCreate): Promise<Consumable> => fetchAPI('/consumables', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: ConsumableUpdate): Promise<Consumable> => fetchAPI(`/consumables/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI(`/consumables/${id}`, { method: 'DELETE' }),
  adjust: (id: number, data: ConsumableAdjust): Promise<Consumable> => fetchAPI(`/consumables/${id}/adjust`, { method: 'POST', body: JSON.stringify(data) }),
  lowStock: (): Promise<Consumable[]> => fetchAPI('/consumables/low-stock'),
}
