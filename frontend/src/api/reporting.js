import { fetchAPI } from './client.js'

export const analytics = {
  get: () => fetchAPI('/analytics'),
  timeAccuracy: (days = 30) => fetchAPI(`/analytics/time-accuracy?days=${days}`),
  failures: (days = 30) => fetchAPI(`/analytics/failures?days=${days}`),
}

export const stats = {
  get: (days) => fetchAPI('/stats' + (days ? `?days=${days}` : '')),
}

export const educationReports = {
  getUsageReport: (days = 30) => fetchAPI(`/education/usage-report?days=${days}`),
}

// Scheduled Reports
export const reportSchedules = {
  list: () => fetchAPI('/report-schedules'),
  create: (data) => fetchAPI('/report-schedules', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/report-schedules/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/report-schedules/${id}`, { method: 'DELETE' }),
  runNow: (id) => fetchAPI(`/report-schedules/${id}/run`, { method: 'POST' }),
}
