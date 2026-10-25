import { fetchAPI } from './client'
import type {
  Printer,
  PrinterCreate,
  PrinterUpdate,
  FilamentSlotUpdate,
  ConnectionTestRequest,
  ConnectionTestResult,
  TelemetryDataPoint,
  HmsHistoryResponse,
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

export interface DispatchRecoveryInspection {
  state: string
  release_available: boolean
  attempt_handle?: string
  snapshot_handle?: string
  actions?: { hold: boolean; reconcile_retain: boolean; release: boolean }
  blockers?: string[]
  job_id?: number | null
  job?: { status: string; held: boolean } | null
  receipt_present?: boolean
  observed_print?: { id: number; status: string } | null
  education?: { submission_id: number; status: string; reservation_state: string | null } | null
  guidance?: string
}

export type DispatchRecoveryAction = 'hold' | 'reconcile-retain' | 'release'
export interface DispatchRecoveryDecision {
  outcome: 'held' | 'retained' | 'released'
  held: boolean
  claim_retained: boolean
  refresh_required: boolean
}

export const printers = {
  recoverDispatch: (id: number, action: DispatchRecoveryAction, body: Record<string, unknown>): Promise<DispatchRecoveryDecision> => fetchAPI(`/printers/${id}/dispatch-recovery/${action}`, { method: 'POST', body: JSON.stringify(body) }),
  dispatchRecovery: (id: number): Promise<DispatchRecoveryInspection> => fetchAPI(`/printers/${id}/dispatch-recovery`),
  list: (activeOnly = false, tag = '', orgId: number | null = null): Promise<Printer[]> => fetchAPI('/printers?active_only=' + activeOnly + (tag ? '&tag=' + encodeURIComponent(tag) : '') + (orgId != null ? '&org_id=' + orgId : '')),
  allTags: (): Promise<string[]> => fetchAPI('/printers/tags'),
  get: (id: number): Promise<Printer> => fetchAPI('/printers/' + id),
  create: (data: PrinterCreate): Promise<Printer> => fetchAPI('/printers', { method: 'POST', body: JSON.stringify(data) }),
  update: (id: number, data: PrinterUpdate): Promise<Printer> => fetchAPI('/printers/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  delete: (id: number): Promise<void> => fetchAPI('/printers/' + id, { method: 'DELETE' }),
  reorder: (ids: number[]): Promise<void> => fetchAPI('/printers/reorder', { method: 'POST', body: JSON.stringify({ printer_ids: ids }) }),
  toggleLights: (id: number): Promise<{ lights_on?: boolean; success?: boolean }> => fetchAPI(`/printers/${id}/lights`, { method: 'POST' }),
  updateSlot: (printerId: number, slotNumber: number, data: FilamentSlotUpdate): Promise<void> =>
    fetchAPI('/printers/' + printerId + '/slots/' + slotNumber, { method: 'PATCH', body: JSON.stringify(data) }),
  testConnection: (data: ConnectionTestRequest): Promise<ConnectionTestResult> => fetchAPI('/printers/test-connection', { method: 'POST', body: JSON.stringify(data) }),
  syncAms: (printerId: number): Promise<unknown> => fetchAPI(`/printers/${printerId}/sync-ams`, { method: 'POST' }),
  clearErrors: (printerId: number): Promise<void> => fetchAPI(`/printers/${printerId}/clear-errors`, { method: 'POST' }),
  skipObjects: (printerId: number, objectIds: number[]): Promise<void> => fetchAPI(`/printers/${printerId}/skip-objects`, { method: 'POST', body: JSON.stringify({ object_ids: objectIds }) }),
  setSpeed: (printerId: number, speed: number): Promise<{ lights_on?: boolean; success?: boolean }> => fetchAPI(`/printers/${printerId}/speed`, { method: 'POST', body: JSON.stringify({ speed }) }),
  assignSlotSpool: (printerId: number, slotNumber: number, spoolId: number): Promise<void> => fetchAPI(`/printers/${printerId}/slots/${slotNumber}/assign?spool_id=${spoolId}`, { method: 'POST' }),
  getCameras: (): Promise<Camera[]> => fetchAPI('/cameras'),
}

export const printerTelemetry = {
  get: (printerId: number, hours = 24): Promise<TelemetryDataPoint[]> => fetchAPI(`/printers/${printerId}/telemetry?hours=${hours}`),
  hmsHistory: (printerId: number, days = 30): Promise<HmsHistoryResponse> => fetchAPI(`/printers/${printerId}/hms-history?days=${days}`),
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
export interface PlugPowerReceipt { accepted?: boolean; power_state: boolean | null }
// Keep the same nonce after a lost/error response; a retry may only replay its
// retained result. getRandomValues also works on self-hosted HTTP LAN origins.
const pendingPowerNonces = new Map<string, string>()
const pendingPowerRequests = new Map<string, Promise<PlugPowerReceipt>>()
const newPowerRequestNonce = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('')
const plugCommand = async (printerId: number, action: string, requestNonce?: string): Promise<PlugPowerReceipt> => {
  const key = `${printerId}:${action}`
  const pending = pendingPowerRequests.get(key)
  if (pending) return pending
  const nonce = requestNonce || pendingPowerNonces.get(key) || newPowerRequestNonce()
  pendingPowerNonces.set(key, nonce)
  const operation = fetchAPI<PlugPowerReceipt>(`/printers/${printerId}/plug/${action}`, { method: 'POST', headers: { 'X-Power-Request-Nonce': nonce } })
    .then(receipt => {
      if (pendingPowerNonces.get(key) === nonce) pendingPowerNonces.delete(key)
      return receipt
    }).finally(() => pendingPowerRequests.delete(key))
  pendingPowerRequests.set(key, operation)
  return operation
}
export const plugPowerOn = (printerId: number, requestNonce?: string): Promise<PlugPowerReceipt> => plugCommand(printerId, 'on', requestNonce)
export const plugPowerOff = (printerId: number, requestNonce?: string): Promise<PlugPowerReceipt> => plugCommand(printerId, 'off', requestNonce)
export const plugPowerToggle = (printerId: number, requestNonce?: string): Promise<PlugPowerReceipt> => plugCommand(printerId, 'toggle', requestNonce)
export const getPlugEnergy = (printerId: number): Promise<PlugEnergyData> => fetchAPI(`/printers/${printerId}/plug/energy`)
export const getPlugState = (printerId: number): Promise<PlugState> => fetchAPI(`/printers/${printerId}/plug/state`)

// ---- AMS Environmental Monitoring ----
export const getAmsEnvironment = (printerId: number, hours = 24, unit: number | null = null): Promise<AmsEnvironmentData> => {
  let url = `/printers/${printerId}/ams/environment?hours=${hours}`
  if (unit !== null) url += `&unit=${unit}`
  return fetchAPI(url)
}
export const getAmsCurrent = (printerId: number): Promise<AmsCurrentData> => fetchAPI(`/printers/${printerId}/ams/current`)
