import { fetchAPI } from './client.js'

// ============== Timelapses ==============
export const timelapses = {
  list: (params = {}) => {
    const query = new URLSearchParams()
    if (params.printer_id) query.set('printer_id', params.printer_id)
    if (params.status) query.set('status', params.status)
    if (params.limit) query.set('limit', params.limit)
    if (params.offset) query.set('offset', params.offset)
    const qs = query.toString()
    return fetchAPI(`/timelapses${qs ? '?' + qs : ''}`)
  },
  videoUrl: (id) => `/api/timelapses/${id}/video`,
  streamUrl: (id) => `/api/timelapses/${id}/stream`,
  downloadUrl: (id) => `/api/timelapses/${id}/download`,
  trim: (id, startSeconds, endSeconds) => fetchAPI(`/timelapses/${id}/trim`, {
    method: 'POST', body: JSON.stringify({ start_seconds: startSeconds, end_seconds: endSeconds }),
  }),
  speed: (id, multiplier) => fetchAPI(`/timelapses/${id}/speed`, {
    method: 'POST', body: JSON.stringify({ multiplier }),
  }),
  delete: (id) => fetchAPI(`/timelapses/${id}`, { method: 'DELETE' }),
}
