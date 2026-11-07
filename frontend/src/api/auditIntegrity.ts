import { fetchAPI } from './client'

export type SiemDestination = {
  id: string; name: string; kind: 'webhook' | 'syslog'; endpoint: string;
  enabled: boolean | number; tls: boolean | number; has_secret: number; ca_bundle: string | null;
  controlled_acknowledgement: { user_id: number; acknowledged_at: string } | null;
  pending: number; last_error: string | null; last_success: string | null;
}
export type DestinationInput = { name: string; kind: 'webhook' | 'syslog'; endpoint: string; enabled: boolean; tls: boolean; secret?: string; ca_bundle?: string | null }
export const auditIntegrity = {
  verify: () => fetchAPI<{ ok: boolean; first_break: number | null; reason?: string; status?: string; rows?: number; breaks?: { reason: string; row_id: number | null; fingerprint: string; known: boolean }[] }>('/audit-logs/verify'),
  initializeKeyring: (reason: string) => fetchAPI('/audit-logs/initialize-keyring', { method: 'POST', body: JSON.stringify({ reason }) }),
  acceptRestoreBaseline: (reason: string) => fetchAPI('/audit-logs/accept-restore-baseline', { method: 'POST', body: JSON.stringify({ reason }) }),
  list: () => fetchAPI<SiemDestination[]>('/settings/siem'),
  save: (id: string, body: DestinationInput) => fetchAPI('/settings/siem/' + id, { method: 'PUT', body: JSON.stringify(body) }),
  acknowledge: (id: string) => fetchAPI('/settings/siem/' + id + '/acknowledge', { method: 'POST', headers: { 'X-ODIN-Controlled-Ack': 'true' } }),
  remove: (id: string) => fetchAPI('/settings/siem/' + id, { method: 'DELETE' }),
}
