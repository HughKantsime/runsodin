import { fetchAPI } from './client'
import type { Timelapse, TimelapseListParams, TimelapseList } from '../types'

export type TimelapseEditReceipt = {
  operation_id: string; phase: 'complete'; kind: 'trim' | 'speed'; result_tid: number;
  source_nonce: string; source_lifetime: string; result_nonce: string; result_lifetime: string;
  frame_count: number; duration_seconds: number; file_size_mb: number; completed_at: string;
  playback_available: boolean; playback_unavailable_reason: string | null;
}
function mediaVersion(path: string, etag?: string) { return path + (etag ? `?version=${encodeURIComponent(etag)}` : '') }
// ============== Timelapses ==============
export const timelapses = {
  list: (params: TimelapseListParams = {}): Promise<TimelapseList> => {
    const query = new URLSearchParams()
    if (params.printer_id) query.set('printer_id', String(params.printer_id))
    if (params.status) query.set('status', params.status)
    if (params.limit) query.set('limit', String(params.limit))
    if (params.offset) query.set('offset', String(params.offset))
    const qs = query.toString()
    return fetchAPI(`/timelapses${qs ? '?' + qs : ''}`)
  },
  videoUrl: (id: number): string => `/api/timelapses/${id}/video`,
  streamUrl: (id: number, etag?: string): string => mediaVersion(`/api/timelapses/${id}/stream`, etag),
  downloadUrl: (id: number, etag?: string): string => mediaVersion(`/api/timelapses/${id}/download`, etag),
  trim: (id: number, startSeconds: number, endSeconds: number, etag: string, operationId: string, signal?: AbortSignal): Promise<TimelapseEditReceipt> => timelapses.edit(id, 'trim', { start_seconds: startSeconds, end_seconds: endSeconds }, etag, operationId, signal),
  speed: (id: number, multiplier: number, etag: string, operationId: string, signal?: AbortSignal): Promise<TimelapseEditReceipt> => timelapses.edit(id, 'speed', { multiplier }, etag, operationId, signal),
  edit: async (id: number, kind: 'trim' | 'speed', body: Record<string, number>, etag: string, operationId: string, signal?: AbortSignal): Promise<TimelapseEditReceipt> => {
    const response = await fetch(`/api/timelapses/${id}/${kind}`, { method: 'POST', credentials: 'include', signal, headers: { 'Content-Type': 'application/json', 'If-Match': etag }, body: JSON.stringify({ ...body, operation_id: operationId }) })
    if (!response.ok) throw await timelapseError(response)
    const receipt: TimelapseEditReceipt = await response.json()
    if (receipt.operation_id !== operationId || receipt.phase !== 'complete') throw new Error('Edit completion was not confirmed')
    return receipt
  },
  editReceipt: async (id: number, operationId: string, signal?: AbortSignal): Promise<TimelapseEditReceipt> => {
    const response = await fetch(`/api/timelapses/${id}/edits/${operationId}`, { credentials: 'include', signal })
    if (!response.ok) throw await timelapseError(response)
    const receipt: TimelapseEditReceipt = await response.json()
    if (receipt.operation_id !== operationId || receipt.phase !== 'complete') throw new Error('Previous edit is unresolved')
    return receipt
  },
  detail: async (id: number, signal?: AbortSignal): Promise<{ row: Timelapse; etag: string }> => {
    const response = await fetch(`/api/timelapses/${id}`, { credentials: 'include', signal })
    if (!response.ok) throw await timelapseError(response)
    const row: Timelapse = await response.json()
    const etag = response.headers.get('ETag') || ''
    if (row.id !== id || !/^"[A-Za-z0-9_-]{1,64}"$/.test(etag)) throw new Error('This recording has no verified lifetime precondition. Deletion is unavailable.')
    return { row, etag }
  },
  checkMedia: async (id: number, signal?: AbortSignal): Promise<void> => {
    const response = await fetch(`/api/timelapses/${id}/download`, { method: 'HEAD', credentials: 'include', signal })
    if (!response.ok) throw new Error(`Media unavailable (${response.status}). Historical recordings without verified lifetime provenance are refused. Check access or retry.`)
  },
  delete: async (id: number, etag: string, signal?: AbortSignal): Promise<void> => {
    if (!/^"[A-Za-z0-9_-]{1,64}"$/.test(etag)) throw new Error('Exact lifetime precondition required')
    const response = await fetch(`/api/timelapses/${id}`, { method: 'DELETE', credentials: 'include', headers: { 'If-Match': etag }, signal })
    if (!response.ok) throw await timelapseError(response)
    if (response.status !== 204) throw new Error('Retirement completion was not confirmed')
  },
}

async function timelapseError(response: Response) {
  const body = await response.json().catch(() => ({}))
  return Object.assign(new Error(body.detail || `Timelapse request refused (${response.status})`), { status: response.status })
}
