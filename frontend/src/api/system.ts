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
  created_at: string;
}

export interface AdminLogEntry {
  timestamp: string;
  level: string;
  message: string;
  source: string;
}

export interface RetentionConfig {
  telemetry_days: number;
  alert_days: number;
  audit_log_days: number;
  archive_days: number;
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
  get: (source = 'backend', lines = 200, level: string | null = null, search: string | null = null): Promise<AdminLogEntry[]> => {
    const params = new URLSearchParams({ source, lines: String(lines) })
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
