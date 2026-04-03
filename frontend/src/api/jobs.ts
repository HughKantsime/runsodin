import { fetchAPI } from './client'
import type {
  Job,
  JobCreate,
  JobUpdate,
  JobStatus,
  ScheduleResult,
  TimelineResponse,
  PrintJob,
  PrintJobStats,
  PrintFile,
  Preset,
  PresetCreate,
  FailureReason,
  ApprovalSetting,
} from '../types'

const API_BASE = '/api'

export const jobs = {
  list: (status?: JobStatus, orgId: number | null = null): Promise<Job[]> => fetchAPI('/jobs' + (status ? '?status=' + status : '') + (orgId != null ? (status ? '&' : '?') + 'org_id=' + orgId : '')),
  get: (id: number): Promise<Job> => fetchAPI('/jobs/' + id),
  create: (data: JobCreate): Promise<Job> => fetchAPI('/jobs', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: JobUpdate): Promise<Job> => fetchAPI('/jobs/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI('/jobs/' + id, { method: 'DELETE' }),
  start: (id: number): Promise<Job> => fetchAPI('/jobs/' + id + '/start', { method: 'POST' }),
  complete: (id: number): Promise<Job> => fetchAPI('/jobs/' + id + '/complete', { method: 'POST' }),
  cancel: (id: number): Promise<Job> => fetchAPI('/jobs/' + id + '/cancel', { method: 'POST' }),
  reorder: (jobIds: number[]): Promise<void> => fetchAPI('/jobs/reorder', { method: 'PATCH', body: JSON.stringify({ job_ids: jobIds }) }),
  repeat: (id: number): Promise<Job> => fetchAPI('/jobs/' + id + '/repeat', { method: 'POST' }),
  dispatch: (id: number): Promise<Job> => fetchAPI('/jobs/' + id + '/dispatch', { method: 'POST' }),
}

export const scheduler = {
  run: (): Promise<ScheduleResult> => fetchAPI('/scheduler/run', { method: 'POST' }),
  getTimeline: (days = 7): Promise<TimelineResponse> => fetchAPI('/timeline?days=' + days),
}

export const timeline = {
  get: (startDate?: string, days = 7): Promise<TimelineResponse> => fetchAPI('/timeline?days=' + days + (startDate ? '&start_date=' + startDate : '')),
}

export const jobActions = {
  move: (jobId: number, printerId: number, scheduledStart?: string): Promise<Job> =>
    fetchAPI('/jobs/' + jobId + '/move', {
      method: 'PATCH',
      body: JSON.stringify({ printer_id: printerId, scheduled_start: scheduledStart })
    }),
}

// Print Jobs (MQTT tracked)
export const printJobs = {
  list: (params: Record<string, string | number> = {}): Promise<PrintJob[]> => {
    const query = new URLSearchParams(params as Record<string, string>).toString()
    return fetchAPI('/print-jobs' + (query ? '?' + query : ''))
  },
  get: (id: number): Promise<PrintJob> => fetchAPI(`/print-jobs/${id}`),
  stats: (): Promise<PrintJobStats> => fetchAPI('/print-jobs/stats'),
}

export const printFiles = {
  upload: async (file: File): Promise<PrintFile> => {
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
      return undefined as unknown as PrintFile
    }
    if (!response.ok) {
      const err = await response.json()
      throw new Error(err.detail || 'Upload failed')
    }
    return response.json()
  },
  list: (): Promise<PrintFile[]> => fetchAPI('/print-files'),
  get: (id: number): Promise<PrintFile> => fetchAPI(`/print-files/${id}`),
  delete: (id: number): Promise<void> => fetchAPI(`/print-files/${id}`, { method: 'DELETE' }),
  schedule: (fileId: number, printerId?: number): Promise<Job> => {
    const url = printerId
      ? `/print-files/${fileId}/schedule?printer_id=${printerId}`
      : `/print-files/${fileId}/schedule`
    return fetchAPI(url, { method: 'POST' })
  }
}

export const presets = {
  list: (): Promise<Preset[]> => fetchAPI('/presets'),
  create: (data: PresetCreate): Promise<Preset> => fetchAPI('/presets', { method: 'POST', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI(`/presets/${id}`, { method: 'DELETE' }),
  schedule: (id: number): Promise<unknown> => fetchAPI(`/presets/${id}/schedule`, { method: 'POST' }),
}

// === Job Approval Workflow ===

export async function approveJob(jobId: number): Promise<Job> {
  return fetchAPI(`/jobs/${jobId}/approve`, { method: 'POST' });
}

export async function rejectJob(jobId: number, reason: string): Promise<Job> {
  return fetchAPI(`/jobs/${jobId}/reject`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  });
}

export async function resubmitJob(jobId: number): Promise<Job> {
  return fetchAPI(`/jobs/${jobId}/resubmit`, { method: 'POST' });
}

export async function getApprovalSetting(): Promise<ApprovalSetting> {
  return fetchAPI('/config/require-job-approval');
}

export async function setApprovalSetting(enabled: boolean): Promise<ApprovalSetting> {
  return fetchAPI('/config/require-job-approval', {
    method: 'PUT',
    body: JSON.stringify({ enabled }),
  });
}

// Failure logging
export async function getFailureReasons(): Promise<FailureReason[]> {
  return fetchAPI('/failure-reasons');
}

export async function updateJobFailure(jobId: number, failReason: string, failNotes: string): Promise<Job> {
  return fetchAPI('/jobs/' + jobId + '/failure', {
    method: 'PATCH',
    body: JSON.stringify({ fail_reason: failReason, fail_notes: failNotes }),
  });
}
