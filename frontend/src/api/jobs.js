import { fetchAPI } from './client.js'

const API_BASE = '/api'

export const jobs = {
  list: (status, orgId = null) => fetchAPI('/jobs' + (status ? '?status=' + status : '') + (orgId != null ? (status ? '&' : '?') + 'org_id=' + orgId : '')),
  get: (id) => fetchAPI('/jobs/' + id),
  create: (data) => fetchAPI('/jobs', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI('/jobs/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI('/jobs/' + id, { method: 'DELETE' }),
  start: (id) => fetchAPI('/jobs/' + id + '/start', { method: 'POST' }),
  complete: (id) => fetchAPI('/jobs/' + id + '/complete', { method: 'POST' }),
  cancel: (id) => fetchAPI('/jobs/' + id + '/cancel', { method: 'POST' }),
  reorder: (jobIds) => fetchAPI('/jobs/reorder', { method: 'PATCH', body: JSON.stringify({ job_ids: jobIds }) }),
  repeat: (id) => fetchAPI('/jobs/' + id + '/repeat', { method: 'POST' }),
  dispatch: (id) => fetchAPI('/jobs/' + id + '/dispatch', { method: 'POST' }),
}

export const scheduler = {
  run: () => fetchAPI('/scheduler/run', { method: 'POST' }),
  getTimeline: (days = 7) => fetchAPI('/timeline?days=' + days),
}

export const timeline = {
  get: (startDate, days = 7) => fetchAPI('/timeline?days=' + days + (startDate ? '&start_date=' + startDate : '')),
}

export const jobActions = {
  move: (jobId, printerId, scheduledStart) =>
    fetchAPI('/jobs/' + jobId + '/move', {
      method: 'PATCH',
      body: JSON.stringify({ printer_id: printerId, scheduled_start: scheduledStart })
    }),
}

// Print Jobs (MQTT tracked)
export const printJobs = {
  list: (params = {}) => {
    const query = new URLSearchParams(params).toString()
    return fetchAPI('/print-jobs' + (query ? '?' + query : ''))
  },
  get: (id) => fetchAPI(`/print-jobs/${id}`),
  stats: () => fetchAPI('/print-jobs/stats'),
}

export const printFiles = {
  upload: async (file) => {
    const formData = new FormData()
    formData.append('file', file)
    const response = await fetch(`${API_BASE}/print-files/upload`, {
      method: 'POST',
      credentials: 'include',
      body: formData
    })
    if (response.status === 401) {
      if (window.location.pathname !== "/login" && window.location.pathname !== "/setup") {
        window.location.href = "/login"
      }
      return
    }
    if (!response.ok) {
      const err = await response.json()
      throw new Error(err.detail || 'Upload failed')
    }
    return response.json()
  },
  list: () => fetchAPI('/print-files'),
  get: (id) => fetchAPI(`/print-files/${id}`),
  delete: (id) => fetchAPI(`/print-files/${id}`, { method: 'DELETE' }),
  schedule: (fileId, printerId) => {
    const url = printerId
      ? `/print-files/${fileId}/schedule?printer_id=${printerId}`
      : `/print-files/${fileId}/schedule`
    return fetchAPI(url, { method: 'POST' })
  }
}

export const presets = {
  list: () => fetchAPI('/presets'),
  create: (data) => fetchAPI('/presets', { method: 'POST', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/presets/${id}`, { method: 'DELETE' }),
  schedule: (id) => fetchAPI(`/presets/${id}/schedule`, { method: 'POST' }),
}

// === Job Approval Workflow ===

export async function approveJob(jobId) {
  return fetchAPI(`/jobs/${jobId}/approve`, { method: 'POST' });
}

export async function rejectJob(jobId, reason) {
  return fetchAPI(`/jobs/${jobId}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
}

export async function resubmitJob(jobId) {
  return fetchAPI(`/jobs/${jobId}/resubmit`, { method: 'POST' });
}

export async function getApprovalSetting() {
  return fetchAPI('/config/require-job-approval');
}

export async function setApprovalSetting(enabled) {
  return fetchAPI('/config/require-job-approval', {
    method: 'PUT',
    body: JSON.stringify({ enabled }),
  });
}

// Failure logging
export async function getFailureReasons() {
  return fetchAPI('/failure-reasons');
}

export async function updateJobFailure(jobId, failReason, failNotes) {
  return fetchAPI('/jobs/' + jobId + '/failure', {
    method: 'PATCH',
    body: JSON.stringify({ fail_reason: failReason, fail_notes: failNotes }),
  });
}
