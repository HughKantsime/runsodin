import { fetchAPI } from './client.js'

const API_BASE = '/api'

export const vision = {
  getDetections: (params) => fetchAPI('/vision/detections?' + new URLSearchParams(params)),
  getDetection: (id) => fetchAPI('/vision/detections/' + id),
  reviewDetection: (id, status) => fetchAPI(`/vision/detections/${id}`, { method: 'PATCH', body: JSON.stringify({ status }) }),
  getSettings: () => fetchAPI('/vision/settings'),
  updateSettings: (data) => fetchAPI('/vision/settings', { method: 'PATCH', body: JSON.stringify(data) }),
  getPrinterSettings: (id) => fetchAPI(`/printers/${id}/vision`),
  updatePrinterSettings: (id, data) => fetchAPI(`/printers/${id}/vision`, { method: 'PATCH', body: JSON.stringify(data) }),
  getModels: () => fetchAPI('/vision/models'),
  activateModel: (id) => fetchAPI(`/vision/models/${id}/activate`, { method: 'PATCH' }),
  uploadModel: async (file, name, detectionType, inputSize = 640) => {
    const formData = new FormData()
    formData.append('file', file)
    const response = await fetch(`${API_BASE}/vision/models?name=${encodeURIComponent(name)}&detection_type=${detectionType}&input_size=${inputSize}`, {
      method: 'POST',
      credentials: 'include',
      body: formData,
    })
    if (!response.ok) throw new Error('Failed to upload model')
    return response.json()
  },
  getStats: (days) => fetchAPI('/vision/stats?days=' + (days || 7)),
}
