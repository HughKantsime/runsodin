import { fetchAPI } from './client'

export type RetentionKind = 'files' | 'timelapses' | 'thumbnails' | 'portfolio'
export interface TermPolicy {
  id: string; name: string; cost_center_id: number; starts_at: string; ends_at: string
  files_days: number | null; timelapses_days: number | null
  thumbnails_days: number | null; portfolio_days: number | null
}
export interface TermPreview {
  after_id: number; next_after_id: number | null; fingerprint: string; due: Record<RetentionKind, boolean>; eligible_student_ids: number[]
  eligible_students: Array<{ id: number; username: string }>
  items: Array<{ submission_id: number; user_id: number; kinds: RetentionKind[]; blocked_reason: string | null; blocked_kinds: Partial<Record<RetentionKind, string>> }>
}
export const termRetention = {
  list: (): Promise<{ items: TermPolicy[]; pending_erasures: Array<{ user_id: number; username: string }> }> => fetchAPI('/education/term-retention'),
  resumeErasure: (userId: number): Promise<{ user_id: number; state: string; reason?: string }> =>
    fetchAPI(`/education/term-retention/erasures/${userId}/resume`, { method: 'POST' }),
  create: (data: Omit<TermPolicy, 'id'>): Promise<TermPolicy> => fetchAPI('/education/term-retention', { method: 'POST', body: JSON.stringify(data) }),
  remove: (id: string): Promise<{ status: string }> => fetchAPI(`/education/term-retention/${id}`, { method: 'DELETE' }),
  preview: (id: string, afterId = 0): Promise<TermPreview> => fetchAPI(`/education/term-retention/${id}/dry-run?after_id=${afterId}`),
  purge: (id: string, fingerprint: string, after_id = 0): Promise<{ next_after_id: number | null; results: Array<{ submission_id: number; kind: string; state: string; reason?: string }> }> =>
    fetchAPI(`/education/term-retention/${id}/purge`, { method: 'POST', body: JSON.stringify({ fingerprint, after_id }) }),
  anonymize: (id: string, fingerprint: string, user_ids: number[], after_id = 0): Promise<{ results: Array<{ user_id: number; state: string; reason?: string }> }> =>
    fetchAPI(`/education/term-retention/${id}/anonymize`, { method: 'POST', body: JSON.stringify({ fingerprint, user_ids, after_id }) }),
}
