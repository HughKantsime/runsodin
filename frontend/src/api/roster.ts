import { fetchAPI } from './client'
export type RosterConnection = {
  id: string; provider: 'google' | 'canvas'; enabled: boolean; revision: number
  config: Record<string, string | string[]>; connected: boolean
}
export type AccountConfirmation = { subject: string; user_id: number; mode: 'identity' | 'grant_only'; decision: 'confirm' | 'decline' }
const root = '/education/roster'
export const roster = {
  settings: () => fetchAPI<{ items: RosterConnection[] }>(root),
  save: (body: unknown) => fetchAPI<RosterConnection>(root, { method: 'PUT', body: JSON.stringify(body) }),
  connect: (id: string) => fetchAPI<{ authorization_url: string }>(`${root}/${id}/connect`, { method: 'POST' }),
  clearTombstone: (id: string, preview_token: string, clear_handle: string) => fetchAPI<{ cleared: boolean }>(`${root}/${id}/tombstones/clear`, { method: 'POST', body: JSON.stringify({ preview_token, clear_handle }) }),
  courses: (id: string) => fetchAPI<{ items: { id: string; title: string }[] }>(`${root}/${id}/courses`),
  preview: (id: string, course_id: string) => fetchAPI<any>(`${root}/${id}/preview`, { method: 'POST', body: JSON.stringify({ course_id }) }),
  apply: (id: string, preview_token: string, account_confirmations: AccountConfirmation[] = []) => fetchAPI<{ class_id: string; skipped?: { reason: string }[] }>(`${root}/${id}/import`, { method: 'POST', body: JSON.stringify({ preview_token, account_confirmations }) }),
}
