import { fetchAPI } from './client'
export interface MaintenanceState { state: 'ok' | 'warn' | 'hold'; note: string; set_by: number | null; set_by_name: string | null; set_at: string | null }
export const printerMaintenance = {
  get: (id: number) => fetchAPI<MaintenanceState>(`/printers/${id}/maintenance-state`),
  set: (id: number, data: Pick<MaintenanceState, 'state' | 'note'>) => fetchAPI<MaintenanceState>(`/printers/${id}/maintenance-state`, { method: 'PUT', body: JSON.stringify(data) }),
}
