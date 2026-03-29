import { fetchAPI } from './client'
import type { Timelapse, TimelapseListParams } from '../types'

// ============== Timelapses ==============
export const timelapses = {
  list: (params: TimelapseListParams = {}): Promise<Timelapse[]> => {
    const query = new URLSearchParams()
    if (params.printer_id) query.set('printer_id', String(params.printer_id))
    if (params.status) query.set('status', params.status)
    if (params.limit) query.set('limit', String(params.limit))
    if (params.offset) query.set('offset', String(params.offset))
    const qs = query.toString()
    return fetchAPI(`/timelapses${qs ? '?' + qs : ''}`)
  },
  videoUrl: (id: number): string => `/api/timelapses/${id}/video`,
  streamUrl: (id: number): string => `/api/timelapses/${id}/stream`,
  downloadUrl: (id: number): string => `/api/timelapses/${id}/download`,
  trim: (id: number, startSeconds: number, endSeconds: number): Promise<Timelapse> => fetchAPI(`/timelapses/${id}/trim`, {
    method: 'POST', body: JSON.stringify({ start_seconds: startSeconds, end_seconds: endSeconds }),
  }),
  speed: (id: number, multiplier: number): Promise<Timelapse> => fetchAPI(`/timelapses/${id}/speed`, {
    method: 'POST', body: JSON.stringify({ multiplier }),
  }),
  delete: (id: number): Promise<void> => fetchAPI(`/timelapses/${id}`, { method: 'DELETE' }),
}
