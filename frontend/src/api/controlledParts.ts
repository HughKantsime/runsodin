import { fetchAPI } from './client'
export type ControlledPolicy = { label: string; audience_id: number; printer_tag: string; retention_days: number }
export type ControlledMark = ControlledPolicy & { controlled: boolean; reason: string; acknowledge_siem?: boolean }
export const controlledParts = {
  get: (kind: 'files' | 'jobs', id: number) => fetchAPI<{ controlled: boolean; policies: ControlledPolicy[] }>(`/controlled-parts/${kind}/${id}`),
  set: (kind: 'files' | 'jobs', id: number, mark: ControlledMark) => fetchAPI(`/controlled-parts/${kind}/${id}`, { method: 'PUT', body: JSON.stringify(mark) }),
  groups: () => fetchAPI<{ id: number; name: string }[]>('/controlled-parts/audiences/eligible'),
}
