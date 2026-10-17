import { fetchAPI } from './client'
import type { SmtpConfig, SmtpConfigResponse } from '../types'

const API_BASE = '/api'

// ---- System types ----

export interface AppConfig {
  [key: string]: unknown;
}

export interface NetworkConfig {
  [key: string]: unknown;
}

export interface BackupEntry {
  filename: string;
  size: number;
  size_bytes?: number;
  created_at: string;
  /** A staged PostgreSQL restore the startup coordinator refused; the database was not changed. */
  quarantined?: boolean;
  reason?: string | null;
  action?: string | null;
}

export interface SystemNotice {
  kind: 'restore_quarantined' | 'license_trial_ending';
  severity: 'critical' | 'warning';
  title: string;
  message: string;
  archive?: string;
  failed_at?: string | null;
  link: string;
  // license_trial_ending only
  buy_url?: string;
  expires_at?: string;
  days_left?: number;
  dismissible?: boolean;
}

/** Superadmin-only notices derived from current server state (GET /system/notices). */
export const systemNotices = {
  list: (): Promise<{ notices: SystemNotice[] }> => fetchAPI('/system/notices'),
  dismiss: (kind: SystemNotice['kind']): Promise<{ status: string; kind: string }> =>
    fetchAPI(`/system/notices/${encodeURIComponent(kind)}/dismiss`, { method: 'POST' }),
}

export interface StagedRestore {
  status: string;
  message: string;
  restart_required: boolean;
  candidate_sha256?: string;
}

export interface AdminLogEntry {
  timestamp: string;
  level: string;
  message: string;
  source: string;
}

export interface RetentionConfig {
  completed_jobs_days: number;
  audit_logs_days: number;
  timelapses_days: number;
  alert_history_days: number;
  [key: string]: unknown;
}

export interface EnergyRateConfig {
  energy_cost_per_kwh: number;
}

export interface EducationModeConfig {
  enabled: boolean;
}

export interface LanguageConfig {
  language: string;
}

// Config
export const config = {
  get: (): Promise<AppConfig> => fetchAPI('/config'),
  update: (data: Partial<AppConfig>): Promise<AppConfig> => fetchAPI('/config', { method: 'PUT', body: JSON.stringify(data) }),
  getNetwork: (): Promise<NetworkConfig> => fetchAPI('/setup/network'),
  saveNetwork: (data: NetworkConfig): Promise<NetworkConfig> => fetchAPI('/setup/network', { method: 'POST', body: JSON.stringify(data) }),
  testSpoolman: (): Promise<unknown> => fetchAPI('/spoolman/test'),
}

// Backups
export const backups = {
  list: (): Promise<BackupEntry[]> => fetchAPI('/backups'),
  create: (): Promise<BackupEntry> => fetchAPI('/backups', { method: 'POST' }),
  remove: (filename: string): Promise<void> => fetchAPI(`/backups/${filename}`, { method: 'DELETE' }),
  stage: (filename: string): Promise<StagedRestore> => fetchAPI(`/backups/${encodeURIComponent(filename)}/stage`, { method: 'POST' }),
  download: async (filename: string): Promise<void> => {
    const res = await fetch(`${API_BASE}/backups/${filename}`, { credentials: 'include' })
    if (!res.ok) throw new Error('Download failed')
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  },
}

export const adminBundle = {
  download: (): Promise<void> => fetch('/api/admin/support-bundle', { credentials: 'include' })
    .then(res => {
      if (!res.ok) throw new Error('Failed to generate bundle')
      return res.blob()
    })
    .then(blob => {
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `odin-support-bundle-${new Date().toISOString().slice(0, 10)}.zip`
      a.click()
      URL.revokeObjectURL(url)
    }),
};

export const adminLogs = {
  get: (source = 'backend', lines = 200, level: string | null = null, search: string | null = null, cursor: string | null = null): Promise<{ lines: string[]; source: string; next_cursor?: string | null; page_cursor?: string | null }> => {
    const params = new URLSearchParams({ source, lines: String(lines) })
    if (cursor) params.set('cursor', cursor)
    if (level) params.set('level', level)
    if (search) params.set('search', search)
    return fetchAPI(`/admin/logs?${params}`)
  },
  streamUrl: (source = 'backend', level: string | null = null, search: string | null = null): string => {
    const params = new URLSearchParams({ source })
    if (level) params.set('level', level)
    if (search) params.set('search', search)
    return `/api/admin/logs/stream?${params}`
  },
};

export const smtpConfig = {
  get: (): Promise<SmtpConfigResponse> => fetchAPI('/smtp-config'),
  update: (data: Partial<SmtpConfig>): Promise<SmtpConfigResponse> => fetchAPI('/smtp-config', {
    method: 'PUT',
    body: JSON.stringify(data),
  }),
  testEmail: (): Promise<unknown> => fetchAPI('/alerts/test-email', { method: 'POST' }),
};

// Data Retention
export const retention = {
  get: (): Promise<RetentionConfig> => fetchAPI('/config/retention'),
  set: (data: Partial<RetentionConfig>): Promise<RetentionConfig> => fetchAPI('/config/retention', {
    method: 'PUT', body: JSON.stringify(data)
  }),
  runCleanup: (): Promise<unknown> => fetchAPI('/admin/retention/cleanup', { method: 'POST' }),
}

export const getEnergyRate = (): Promise<EnergyRateConfig> => fetchAPI('/settings/energy-rate')
export const setEnergyRate = (rate: number): Promise<EnergyRateConfig> => fetchAPI('/settings/energy-rate', { method: 'PUT', body: JSON.stringify({ energy_cost_per_kwh: rate }) })

// ---- Education Mode ----
export const getEducationMode = (): Promise<EducationModeConfig> => fetchAPI('/settings/education-mode')
export const setEducationMode = (enabled: boolean): Promise<EducationModeConfig> => fetchAPI('/settings/education-mode', { method: 'PUT', body: JSON.stringify({ enabled }) })

// ---- Language / i18n ----
export const getLanguage = (): Promise<LanguageConfig> => fetchAPI('/settings/language')
export const setLanguage = (lang: string): Promise<LanguageConfig> => fetchAPI('/settings/language', { method: 'PUT', body: JSON.stringify({ language: lang }) })

// ---- Diagnostics ----
export const diagnostics = {
  fetch: (): Promise<Record<string, unknown>> => fetchAPI('/v1/system/diagnostics'),
  download: async (): Promise<void> => {
    const data = await fetchAPI<Record<string, unknown>>('/v1/system/diagnostics')
    const version = (data.odin_version as string) || 'unknown'
    const timestamp = new Date().toISOString().slice(0, 19).replace(/:/g, '-')
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `odin-diagnostics-${version}-${timestamp}.json`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  },
}
