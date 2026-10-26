import { fetchAPI } from './client'
export interface Suggestion {
  job_id: number | null; reason: string; filament: 'no_change' | 'change_needed' | 'verify_needed';
  fingerprint: string | null; load?: { slot: number; material: string; color: string }[]
}
export interface CompletionSummary {
  job_id: number; job_name: string; printer_id: number | null; printer: string;
  actual_print_seconds: number | null; recipe_state: string; product: string[];
  expected_outputs: { product_id: number; product_name: string; kind: string; expected: number }[];
  health: string; health_events: { kind: string; code: string | null }[];
  inspection_available: boolean; inspection_unavailable_reason: string | null;
  square_projection: { product_id: number; quantity: number | null }[]; bed_line: string;
}
export interface TimelineEvent { id: string; kind: string; code: string | null; occurred_at: string; detail: Record<string, unknown> }
export const jobCompletion = {
  suggestion: (id: number) => fetchAPI<Suggestion>(`/printers/${id}/next-suggestion`),
  start: (id: number, data: { suggested_job_id: number; suggestion_fingerprint: string; request_id: string; bed_clear_confirmed: true; attestation: 'owner_in_app' }) =>
    fetchAPI<{ success: boolean; state: string; message?: string }>(`/printers/${id}/start-next`, { method: 'POST', body: JSON.stringify(data) }),
  timeline: (id: number) => fetchAPI<{ events: TimelineEvent[] }>(`/jobs/${id}/timeline`),
}
