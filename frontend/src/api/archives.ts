import { fetchAPI, downloadBlob } from './client'
import type {
  Archive,
  ArchiveUpdate,
  ArchiveListParams,
  ArchiveLogParams,
  ArchiveCompareResult,
  ArchiveAmsPreview,
  ArchiveReprintRequest,
  Project,
  ProjectCreate,
  ProjectUpdate,
  ProjectImportResult,
  Tag,
  Timelapse,
  TimelapseListParams,
} from '../types'
import type { PaginatedResponse } from '../types/common'

export const archives = {
  list: (params: ArchiveListParams = {}): Promise<PaginatedResponse<Archive>> => {
    const q = new URLSearchParams()
    if (params.page) q.set('page', String(params.page))
    if (params.per_page) q.set('per_page', String(params.per_page))
    if (params.printer_id) q.set('printer_id', String(params.printer_id))
    if (params.status) q.set('status', params.status)
    if (params.search) q.set('search', params.search)
    if (params.start_date) q.set('start_date', params.start_date)
    if (params.end_date) q.set('end_date', params.end_date)
    if (params.tag) q.set('tag', params.tag)
    return fetchAPI(`/archives?${q}`)
  },
  get: (id: number): Promise<Archive> => fetchAPI(`/archives/${id}`),
  update: (id: number, data: ArchiveUpdate): Promise<Archive> => fetchAPI(`/archives/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI(`/archives/${id}`, { method: 'DELETE' }),
  updateTags: (id: number, tags: string[]): Promise<Archive> => fetchAPI(`/archives/${id}/tags`, { method: 'PATCH', body: JSON.stringify({ tags }) }),
  compare: (a: number, b: number): Promise<ArchiveCompareResult> => fetchAPI(`/archives/compare?a=${a}&b=${b}`),
  amsPreview: (id: number, printerId?: number): Promise<ArchiveAmsPreview> => fetchAPI(`/archives/${id}/ams-preview${printerId ? `?printer_id=${printerId}` : ''}`),
  reprint: (id: number, data: ArchiveReprintRequest): Promise<{ message: string }> => fetchAPI(`/archives/${id}/reprint`, { method: 'POST', body: JSON.stringify(data) }),
  log: (params: ArchiveLogParams = {}): Promise<PaginatedResponse<Archive>> => {
    const q = new URLSearchParams()
    if (params.page) q.set('page', String(params.page))
    if (params.per_page) q.set('per_page', String(params.per_page))
    if (params.printer_id) q.set('printer_id', String(params.printer_id))
    if (params.user_id) q.set('user_id', String(params.user_id))
    if (params.status) q.set('status', params.status)
    if (params.search) q.set('search', params.search)
    if (params.start_date) q.set('start_date', params.start_date)
    if (params.end_date) q.set('end_date', params.end_date)
    if (params.tag) q.set('tag', params.tag)
    return fetchAPI(`/archives/log?${q}`)
  },
  logExport: async (params: ArchiveLogParams = {}): Promise<string> => {
    const q = new URLSearchParams()
    if (params.printer_id) q.set('printer_id', String(params.printer_id))
    if (params.user_id) q.set('user_id', String(params.user_id))
    if (params.status) q.set('status', params.status)
    if (params.search) q.set('search', params.search)
    if (params.start_date) q.set('start_date', params.start_date)
    if (params.end_date) q.set('end_date', params.end_date)
    if (params.tag) q.set('tag', params.tag)
    const csv = await fetchAPI<string>(`/archives/log/export?${q}`, {}, 'csv')
    if (csv === undefined) throw new Error('Export unavailable. Sign in and try again.')
    if (typeof csv !== 'string') throw new Error('Unexpected export response')
    return csv
  },
  previewModel: (fileId: number): string => `${window.location.origin}/api/files/${fileId}/preview-model`,
};

export const projects = {
  list: (status?: string): Promise<Project[]> => fetchAPI('/projects' + (status ? '?status=' + status : '')),
  get: (id: number): Promise<Project> => fetchAPI(`/projects/${id}`),
  create: (data: ProjectCreate): Promise<Project> => fetchAPI('/projects', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: ProjectUpdate): Promise<Project> => fetchAPI(`/projects/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI(`/projects/${id}`, { method: 'DELETE' }),
  assignArchives: (id: number, archiveIds: number[]): Promise<unknown> => fetchAPI(`/projects/${id}/archives`, { method: 'POST', body: JSON.stringify({ archive_ids: archiveIds }) }),
  removeArchive: (projectId: number, archiveId: number): Promise<void> => fetchAPI(`/projects/${projectId}/archives/${archiveId}`, { method: 'DELETE' }),
  exportZip: (id: number, name: string): Promise<void> =>
    downloadBlob(`/projects/${id}/export`, `project_${name.replace(/[^A-Za-z0-9_-]/g, '_').slice(0, 50)}.zip`),
  importZip: (file: File): Promise<ProjectImportResult> => {
    const body = new FormData()
    body.append('file', file)
    return fetchAPI('/projects/import', { method: 'POST', body })
  },
}

export const tags = {
  list: (): Promise<Tag[]> => fetchAPI('/tags'),
  rename: (oldTag: string, newTag: string): Promise<void> => fetchAPI('/tags/rename', { method: 'POST', body: JSON.stringify({ old: oldTag, new: newTag }) }),
  delete: (tag: string): Promise<void> => fetchAPI(`/tags/${encodeURIComponent(tag)}`, { method: 'DELETE' }),
};
