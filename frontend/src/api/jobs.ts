import { fetchAPI } from './client'
import { dispatchWithConfirmation } from './jobDispatch'
import { clearSensitiveBrowserState } from '../permissions'
import type {
  Job,
  RecentTerminalJob,
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

export interface JobListParams {
  limit?: number
  offset?: number
  order?: 'queue' | 'recent'
  q?: string
  printerId?: number | null
  unassigned?: boolean
  held?: boolean
}

/** Query string for GET /jobs. With nothing set it is empty, which is the call the dashboard and the apps make. */
export function buildJobsQuery(status?: string | string[] | null, orgId: number | null = null, params: JobListParams = {}): string {
  const query = new URLSearchParams()
  const statuses = Array.isArray(status) ? status : status ? [status] : []
  statuses.forEach(value => query.append('status', value))
  if (orgId != null) query.set('org_id', String(orgId))
  if (params.printerId != null) query.set('printer_id', String(params.printerId))
  if (params.unassigned) query.set('unassigned', 'true')
  if (params.held) query.set('held', 'true')
  if (params.q) query.set('q', params.q)
  if (params.order === 'recent') query.set('order', 'recent')
  if (params.limit != null) query.set('limit', String(params.limit))
  if (params.offset) query.set('offset', String(params.offset))
  const text = query.toString()
  return text ? '?' + text : ''
}

export const jobs = {
  recentTerminal: (orgId: number | null = null): Promise<RecentTerminalJob[]> => fetchAPI('/jobs/recent-terminal' + (orgId != null ? '?org_id=' + orgId : '')),
  // onTotal receives the X-Total-Count header (null when the server sends none).
  list: (status?: string | string[] | null, orgId: number | null = null, params: JobListParams = {}, onTotal?: (total: number | null) => void): Promise<Job[]> =>
    fetchAPI('/jobs' + buildJobsQuery(status, orgId, params), onTotal ? { onResponse: (response: Response) => {
      const header = response.headers.get('X-Total-Count')
      const total = header == null ? NaN : Number(header)
      onTotal(Number.isInteger(total) && total >= 0 ? total : null)
    } } : {}),
  get: (id: number): Promise<Job> => fetchAPI('/jobs/' + id),
  create: (data: JobCreate): Promise<Job> => fetchAPI('/jobs', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: JobUpdate): Promise<Job> => fetchAPI('/jobs/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI('/jobs/' + id, { method: 'DELETE' }),
  start: (id: number): Promise<Job> => fetchAPI('/jobs/' + id + '/start', { method: 'POST' }),
  complete: (id: number): Promise<Job> => fetchAPI('/jobs/' + id + '/complete', { method: 'POST' }),
  cancel: (id: number): Promise<Job> => fetchAPI('/jobs/' + id + '/cancel', { method: 'POST' }),
  fail: (id: number): Promise<Job> => fetchAPI('/jobs/' + id + '/fail', { method: 'POST' }),
  reorder: (jobIds: number[]): Promise<void> => fetchAPI('/jobs/reorder', { method: 'PATCH', body: JSON.stringify({ job_ids: jobIds }) }),
  repeat: (id: number): Promise<{ success: boolean; message: string; original_job_id: number; new_job_id: number; held_for_planned_load: boolean }> => fetchAPI('/jobs/' + id + '/repeat', { method: 'POST' }),
  dispatch: dispatchWithConfirmation,
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
  rename: (id: number, original_filename: string): Promise<{id: number; original_filename: string; display_name: string}> => fetchAPI(`/print-files/${id}/name`, {method: 'PATCH', body: JSON.stringify({original_filename})}),
  upload: async (file: File): Promise<PrintFile> => {
    const formData = new FormData()
    formData.append('file', file)
    const response = await fetch(`${API_BASE}/print-files/upload`, {
      method: 'POST',
      credentials: 'include',
      body: formData
    })
    if (response.status === 401) {
      await clearSensitiveBrowserState()
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
  get: (id: number, printerId?: number): Promise<PrintFile> => fetchAPI(`/print-files/${id}${printerId ? `?printer_id=${printerId}` : ""}`),
  delete: (id: number): Promise<void> => fetchAPI(`/print-files/${id}`, { method: 'DELETE' }),
  schedule: (fileId: number, printerId?: number, options?: { ams_mapping?: import("../types/archive").AmsMapping; hold?: boolean }): Promise<Job> => {
    const url = printerId
      ? `/print-files/${fileId}/schedule?printer_id=${printerId}`
      : `/print-files/${fileId}/schedule`
    return fetchAPI(url, { method: 'POST', ...(options ? { body: JSON.stringify(options) } : {}) })
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
