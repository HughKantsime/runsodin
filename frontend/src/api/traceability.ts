import { fetchAPI, downloadBlob } from './client'

export interface TraceabilityRecord {
  job_id: number; attempt: number; captured_at: string; inspection_required: boolean;
  inspection: { inspection_result: 'pass' | 'fail' | null; inspection_notes: string | null; inspected_by: number | null; inspected_at: string | null };
  delivery: { delivered_at: string | null; delivered_by: number | null };
  file: { name: string | null; sha256: string | null };
  [key: string]: unknown;
}
export interface InspectionPolicy { org_id: number; target_kind: 'printer_tag' | 'product'; target_key: string; required: boolean }
export const traceability = {
  get: (id: number) => fetchAPI<TraceabilityRecord>(`/jobs/${id}/traceability`),
  inspect: (id: number, result: 'pass' | 'fail', notes: string) => fetchAPI<TraceabilityRecord>(`/jobs/${id}/inspection`, { method: 'PUT', body: JSON.stringify({ result, notes }) }),
  deliver: (id: number) => fetchAPI<TraceabilityRecord>(`/jobs/${id}/deliver`, { method: 'POST' }),
  certificate: (id: number) => downloadBlob(`/jobs/${id}/certificate?bundle=true`, `job-${id}-certificate.zip`),
  batch: (start: string, end: string) => downloadBlob(`/jobs/traceability/export?${new URLSearchParams({ start: new Date(`${start}T00:00:00`).toISOString(), end: new Date(`${end}T00:00:00`).toISOString() })}`, 'traceability.zip'),
  policies: (org: number) => fetchAPI<InspectionPolicy[]>(`/jobs/traceability/policies?org_id=${org}`),
  policy: (policy: InspectionPolicy) => fetchAPI<InspectionPolicy>('/jobs/traceability/policies', { method: 'PUT', body: JSON.stringify(policy) }),
}
