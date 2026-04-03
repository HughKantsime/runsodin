import { fetchAPI } from './client'
import type {
  Spool,
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
export const spools = {
  list: (filters: SpoolListFilters = {}): Promise<Spool[]> => {
    const params = new URLSearchParams()
    if (filters.status) params.append('status', filters.status)
    if (filters.printer_id) params.append('printer_id', String(filters.printer_id))
    if (filters.org_id != null) params.append('org_id', String(filters.org_id))
    return fetchAPI(`/spools?${params}`)
  },
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
