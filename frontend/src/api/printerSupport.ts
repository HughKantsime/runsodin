import { fetchAPI } from './client'

export interface ExperimentalActionsResult {
  printer_id: number
  support_level: 'supported' | 'experimental' | 'unsupported' | null
  experimental_actions_enabled: boolean
}

export const printerSupport = {
  setAmsHt: (id: number, enabled: boolean) =>
    fetchAPI<{ ams_ht_enabled: boolean }>(`/printers/${id}/experimental-actions`, {
      method: 'PUT', body: JSON.stringify({ capability: 'ams_ht', enabled }),
    }),
  setExperimentalActions: (id: number, enabled: boolean) =>
    fetchAPI<ExperimentalActionsResult>(`/printers/${id}/experimental-actions`, { method: 'PUT', body: JSON.stringify({ enabled }) }),
}
