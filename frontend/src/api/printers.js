import { fetchAPI } from './client.js'

export const printers = {
  list: (activeOnly = false, tag = '', orgId = null) => fetchAPI('/printers?active_only=' + activeOnly + (tag ? '&tag=' + encodeURIComponent(tag) : '') + (orgId != null ? '&org_id=' + orgId : '')),
  allTags: () => fetchAPI('/printers/tags'),
  get: (id) => fetchAPI('/printers/' + id),
  create: (data) => fetchAPI('/printers', { method: 'POST', body: JSON.stringify(data) }),
  update: (id, data) => fetchAPI('/printers/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id) => fetchAPI('/printers/' + id, { method: 'DELETE' }),
  reorder: (ids) => fetchAPI('/printers/reorder', { method: 'POST', body: JSON.stringify({ printer_ids: ids }) }),
  toggleLights: (id) => fetchAPI(`/printers/${id}/lights`, { method: 'POST' }),
  updateSlot: (printerId, slotNumber, data) =>
    fetchAPI('/printers/' + printerId + '/slots/' + slotNumber, { method: 'PATCH', body: JSON.stringify(data) }),
  testConnection: (data) => fetchAPI('/printers/test-connection', { method: 'POST', body: JSON.stringify(data) }),
  syncAms: (printerId) => fetchAPI(`/printers/${printerId}/sync-ams`, { method: 'POST' }),
  clearErrors: (printerId) => fetchAPI(`/printers/${printerId}/clear-errors`, { method: 'POST' }),
  skipObjects: (printerId, objectIds) => fetchAPI(`/printers/${printerId}/skip-objects`, { method: 'POST', body: JSON.stringify({ object_ids: objectIds }) }),
  setSpeed: (printerId, speed) => fetchAPI(`/printers/${printerId}/speed`, { method: 'POST', body: JSON.stringify({ speed }) }),
  assignSlotSpool: (printerId, slotNumber, spoolId) => fetchAPI(`/printers/${printerId}/slots/${slotNumber}/assign?spool_id=${spoolId}`, { method: 'POST' }),
  getCameras: () => fetchAPI('/cameras'),
}

export const printerTelemetry = {
  get: (printerId, hours = 24) => fetchAPI(`/printers/${printerId}/telemetry?hours=${hours}`),
  hmsHistory: (printerId, days = 30) => fetchAPI(`/printers/${printerId}/hms-history?days=${days}`),
  nozzle: (printerId) => fetchAPI(`/printers/${printerId}/nozzle`),
  nozzleStatus: (printerId) => fetchAPI(`/printers/${printerId}/nozzle-status`),
  installNozzle: (printerId, data) => fetchAPI(`/printers/${printerId}/nozzle`, { method: 'POST', body: JSON.stringify(data) }),
  retireNozzle: (printerId, nozzleId) => fetchAPI(`/printers/${printerId}/nozzle/${nozzleId}/retire`, { method: 'PATCH' }),
  nozzleHistory: (printerId) => fetchAPI(`/printers/${printerId}/nozzle/history`),
}

export const maintenance = {
  getStatus: () => fetchAPI('/maintenance/status'),
  getTasks: () => fetchAPI('/maintenance/tasks'),
  createTask: (data) => fetchAPI('/maintenance/tasks', { method: 'POST', body: JSON.stringify(data) }),
  updateTask: (id, data) => fetchAPI('/maintenance/tasks/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteTask: (id) => fetchAPI('/maintenance/tasks/' + id, { method: 'DELETE' }),
  getLogs: (printerId) => fetchAPI('/maintenance/logs' + (printerId ? '?printer_id=' + printerId : '')),
  createLog: (data) => fetchAPI('/maintenance/logs', { method: 'POST', body: JSON.stringify(data) }),
  deleteLog: (id) => fetchAPI('/maintenance/logs/' + id, { method: 'DELETE' }),
  seedDefaults: () => fetchAPI('/maintenance/seed-defaults', { method: 'POST' }),
}

// Bulk Operations
export const bulkOps = {
  jobs: (jobIds, action, extra = {}) => fetchAPI('/jobs/bulk-update', {
    method: 'POST', body: JSON.stringify({ job_ids: jobIds, action, ...extra })
  }),
  printers: (printerIds, action, extra = {}) => fetchAPI('/printers/bulk-update', {
    method: 'POST', body: JSON.stringify({ printer_ids: printerIds, action, ...extra })
  }),
  spools: (spoolIds, action, extra = {}) => fetchAPI('/spools/bulk-update', {
    method: 'POST', body: JSON.stringify({ spool_ids: spoolIds, action, ...extra })
  }),
}

// ---- Smart Plug ----
export const getPlugConfig = (printerId) => fetchAPI(`/printers/${printerId}/plug`)
export const updatePlugConfig = (printerId, config) => fetchAPI(`/printers/${printerId}/plug`, { method: 'PUT', body: JSON.stringify(config) })
export const removePlugConfig = (printerId) => fetchAPI(`/printers/${printerId}/plug`, { method: 'DELETE' })
export const plugPowerOn = (printerId) => fetchAPI(`/printers/${printerId}/plug/on`, { method: 'POST' })
export const plugPowerOff = (printerId) => fetchAPI(`/printers/${printerId}/plug/off`, { method: 'POST' })
export const plugPowerToggle = (printerId) => fetchAPI(`/printers/${printerId}/plug/toggle`, { method: 'POST' })
export const getPlugEnergy = (printerId) => fetchAPI(`/printers/${printerId}/plug/energy`)
export const getPlugState = (printerId) => fetchAPI(`/printers/${printerId}/plug/state`)

// ---- AMS Environmental Monitoring ----
export const getAmsEnvironment = (printerId, hours = 24, unit = null) => {
  let url = `/printers/${printerId}/ams/environment?hours=${hours}`
  if (unit !== null) url += `&unit=${unit}`
  return fetchAPI(url)
}
export const getAmsCurrent = (printerId) => fetchAPI(`/printers/${printerId}/ams/current`)
