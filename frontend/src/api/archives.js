import { fetchAPI } from './client.js'

export const archives = {
  list: (params = {}) => {
    const q = new URLSearchParams()
    if (params.page) q.set('page', params.page)
    if (params.per_page) q.set('per_page', params.per_page)
    if (params.printer_id) q.set('printer_id', params.printer_id)
    if (params.status) q.set('status', params.status)
    if (params.search) q.set('search', params.search)
    if (params.start_date) q.set('start_date', params.start_date)
    if (params.end_date) q.set('end_date', params.end_date)
    if (params.tag) q.set('tag', params.tag)
    return fetchAPI(`/archives?${q}`)
  },
  get: (id) => fetchAPI(`/archives/${id}`),
  update: (id, data) => fetchAPI(`/archives/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/archives/${id}`, { method: 'DELETE' }),
  updateTags: (id, tags) => fetchAPI(`/archives/${id}/tags`, { method: 'PATCH', body: JSON.stringify({ tags }) }),
  compare: (a, b) => fetchAPI(`/archives/compare?a=${a}&b=${b}`),
  amsPreview: (id, printerId) => fetchAPI(`/archives/${id}/ams-preview${printerId ? `?printer_id=${printerId}` : ''}`),
  reprint: (id, data) => fetchAPI(`/archives/${id}/reprint`, { method: 'POST', body: JSON.stringify(data) }),
  log: (params = {}) => {
    const q = new URLSearchParams()
    if (params.page) q.set('page', params.page)
    if (params.per_page) q.set('per_page', params.per_page)
    if (params.printer_id) q.set('printer_id', params.printer_id)
    if (params.user_id) q.set('user_id', params.user_id)
    if (params.status) q.set('status', params.status)
    if (params.search) q.set('search', params.search)
    if (params.start_date) q.set('start_date', params.start_date)
    if (params.end_date) q.set('end_date', params.end_date)
    if (params.tag) q.set('tag', params.tag)
    return fetchAPI(`/archives/log?${q}`)
  },
  logExport: (params = {}) => {
    const q = new URLSearchParams()
    if (params.printer_id) q.set('printer_id', params.printer_id)
    if (params.user_id) q.set('user_id', params.user_id)
    if (params.status) q.set('status', params.status)
    if (params.search) q.set('search', params.search)
    if (params.start_date) q.set('start_date', params.start_date)
    if (params.end_date) q.set('end_date', params.end_date)
    if (params.tag) q.set('tag', params.tag)
    return fetchAPI(`/archives/log/export?${q}`)
  },
  previewModel: (fileId) => `${window.location.origin}/api/files/${fileId}/preview-model`,
};

export const projects = {
  list: (status) => fetchAPI('/projects' + (status ? '?status=' + status : '')),
  get: (id) => fetchAPI(`/projects/${id}`),
  create: (data) => fetchAPI('/projects', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI(`/projects/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI(`/projects/${id}`, { method: 'DELETE' }),
  assignArchives: (id, archiveIds) => fetchAPI(`/projects/${id}/archives`, { method: 'POST', body: JSON.stringify({ archive_ids: archiveIds }) }),
  removeArchive: (projectId, archiveId) => fetchAPI(`/projects/${projectId}/archives/${archiveId}`, { method: 'DELETE' }),
}

export const tags = {
  list: () => fetchAPI('/tags'),
  rename: (oldTag, newTag) => fetchAPI('/tags/rename', { method: 'POST', body: JSON.stringify({ old: oldTag, new: newTag }) }),
  delete: (tag) => fetchAPI(`/tags/${encodeURIComponent(tag)}`, { method: 'DELETE' }),
};
