import { fetchAPI } from './client'
import type {
  Printer,
  PrinterCreate,
  PrinterUpdate,
  FilamentSlotUpdate,
  ConnectionTestRequest,
  ConnectionTestResult,
  TelemetryDataPoint,
  HmsErrorHistoryEntry,
  NozzleInstall,
  NozzleLifecycle,
  MaintenanceTask,
  MaintenanceTaskCreate,
  MaintenanceTaskUpdate,
  MaintenanceLog,
  MaintenanceLogCreate,
  PlugConfig,
  PlugEnergyData,
  PlugState,
  AmsEnvironmentData,
  AmsCurrentData,
  Camera,
} from '../types'

export const printers = {
  list: (activeOnly = false, tag = '', orgId: number | null = null): Promise<Printer[]> => fetchAPI('/printers?active_only=' + activeOnly + (tag ? '&tag=' + encodeURIComponent(tag) : '') + (orgId != null ? '&org_id=' + orgId : '')),
  allTags: (): Promise<string[]> => fetchAPI('/printers/tags'),
  get: (id: number): Promise<Printer> => fetchAPI('/printers/' + id),
  create: (data: PrinterCreate): Promise<Printer> => fetchAPI('/printers', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: PrinterUpdate): Promise<Printer> => fetchAPI('/printers/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI('/printers/' + id, { method: 'DELETE' }),
  reorder: (ids: number[]): Promise<void> => fetchAPI('/printers/reorder', { method: 'POST', body: JSON.stringify({ printer_ids: ids }) }),
  toggleLights: (id: number): Promise<void> => fetchAPI(`/printers/${id}/lights`, { method: 'POST' }),
  updateSlot: (printerId: number, slotNumber: number, data: FilamentSlotUpdate): Promise<void> =>
    fetchAPI('/printers/' + printerId + '/slots/' + slotNumber, { method: 'PATCH', body: JSON.stringify(data) }),
  testConnection: (data: ConnectionTestRequest): Promise<ConnectionTestResult> => fetchAPI('/printers/test-connection', { method: 'POST', body: JSON.stringify(data) }),
  syncAms: (printerId: number): Promise<unknown> => fetchAPI(`/printers/${printerId}/sync-ams`, { method: 'POST' }),
  clearErrors: (printerId: number): Promise<void> => fetchAPI(`/printers/${printerId}/clear-errors`, { method: 'POST' }),
  skipObjects: (printerId: number, objectIds: number[]): Promise<void> => fetchAPI(`/printers/${printerId}/skip-objects`, { method: 'POST', body: JSON.stringify({ object_ids: objectIds }) }),
  setSpeed: (printerId: number, speed: number): Promise<void> => fetchAPI(`/printers/${printerId}/speed`, { method: 'POST', body: JSON.stringify({ speed }) }),
  assignSlotSpool: (printerId: number, slotNumber: number, spoolId: number): Promise<void> => fetchAPI(`/printers/${printerId}/slots/${slotNumber}/assign?spool_id=${spoolId}`, { method: 'POST' }),
  getCameras: (): Promise<Camera[]> => fetchAPI('/cameras'),
}

export const printerTelemetry = {
  get: (printerId: number, hours = 24): Promise<TelemetryDataPoint[]> => fetchAPI(`/printers/${printerId}/telemetry?hours=${hours}`),
  hmsHistory: (printerId: number, days = 30): Promise<HmsErrorHistoryEntry[]> => fetchAPI(`/printers/${printerId}/hms-history?days=${days}`),
  nozzle: (printerId: number): Promise<NozzleLifecycle | null> => fetchAPI(`/printers/${printerId}/nozzle`),
  nozzleStatus: (printerId: number): Promise<unknown> => fetchAPI(`/printers/${printerId}/nozzle-status`),
  installNozzle: (printerId: number, data: NozzleInstall): Promise<NozzleLifecycle> => fetchAPI(`/printers/${printerId}/nozzle`, { method: 'POST', body: JSON.stringify(data) }),
  retireNozzle: (printerId: number, nozzleId: number): Promise<NozzleLifecycle> => fetchAPI(`/printers/${printerId}/nozzle/${nozzleId}/retire`, { method: 'PATCH' }),
  nozzleHistory: (printerId: number): Promise<NozzleLifecycle[]> => fetchAPI(`/printers/${printerId}/nozzle/history`),
}

export const maintenance = {
  getStatus: (): Promise<unknown> => fetchAPI('/maintenance/status'),
  getTasks: (): Promise<MaintenanceTask[]> => fetchAPI('/maintenance/tasks'),
  createTask: (data: MaintenanceTaskCreate): Promise<MaintenanceTask> => fetchAPI('/maintenance/tasks', { method: 'POST', body: JSON.stringify(data) }),
  updateTask: (id: number, data: MaintenanceTaskUpdate): Promise<MaintenanceTask> => fetchAPI('/maintenance/tasks/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteTask: (id: number): Promise<void> => fetchAPI('/maintenance/tasks/' + id, { method: 'DELETE' }),
  getLogs: (printerId?: number): Promise<MaintenanceLog[]> => fetchAPI('/maintenance/logs' + (printerId ? '?printer_id=' + printerId : '')),
  createLog: (data: MaintenanceLogCreate): Promise<MaintenanceLog> => fetchAPI('/maintenance/logs', { method: 'POST', body: JSON.stringify(data) }),
  deleteLog: (id: number): Promise<void> => fetchAPI('/maintenance/logs/' + id, { method: 'DELETE' }),
  seedDefaults: (): Promise<void> => fetchAPI('/maintenance/seed-defaults', { method: 'POST' }),
}

// Bulk Operations
export const bulkOps = {
  jobs: (jobIds: number[], action: string, extra: Record<string, unknown> = {}): Promise<unknown> => fetchAPI('/jobs/bulk-update', {
    method: 'POST', body: JSON.stringify({ job_ids: jobIds, action, ...extra })
  }),
  printers: (printerIds: number[], action: string, extra: Record<string, unknown> = {}): Promise<unknown> => fetchAPI('/printers/bulk-update', {
    method: 'POST', body: JSON.stringify({ printer_ids: printerIds, action, ...extra })
  }),
  spools: (spoolIds: number[], action: string, extra: Record<string, unknown> = {}): Promise<unknown> => fetchAPI('/spools/bulk-update', {
    method: 'POST', body: JSON.stringify({ spool_ids: spoolIds, action, ...extra })
  }),
}

// ---- Smart Plug ----
export const getPlugConfig = (printerId: number): Promise<PlugConfig> => fetchAPI(`/printers/${printerId}/plug`)
export const updatePlugConfig = (printerId: number, config: PlugConfig): Promise<PlugConfig> => fetchAPI(`/printers/${printerId}/plug`, { method: 'PUT', body: JSON.stringify(config) })
export const removePlugConfig = (printerId: number): Promise<void> => fetchAPI(`/printers/${printerId}/plug`, { method: 'DELETE' })
export const plugPowerOn = (printerId: number): Promise<void> => fetchAPI(`/printers/${printerId}/plug/on`, { method: 'POST' })
export const plugPowerOff = (printerId: number): Promise<void> => fetchAPI(`/printers/${printerId}/plug/off`, { method: 'POST' })
export const plugPowerToggle = (printerId: number): Promise<void> => fetchAPI(`/printers/${printerId}/plug/toggle`, { method: 'POST' })
export const getPlugEnergy = (printerId: number): Promise<PlugEnergyData> => fetchAPI(`/printers/${printerId}/plug/energy`)
export const getPlugState = (printerId: number): Promise<PlugState> => fetchAPI(`/printers/${printerId}/plug/state`)

// ---- AMS Environmental Monitoring ----
export const getAmsEnvironment = (printerId: number, hours = 24, unit: number | null = null): Promise<AmsEnvironmentData> => {
  let url = `/printers/${printerId}/ams/environment?hours=${hours}`
  if (unit !== null) url += `&unit=${unit}`
  return fetchAPI(url)
}
export const getAmsCurrent = (printerId: number): Promise<AmsCurrentData> => fetchAPI(`/printers/${printerId}/ams/current`)
