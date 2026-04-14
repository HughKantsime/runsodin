import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Database, HardDrive, Plus, RefreshCw, Download, Trash2, CheckCircle, AlertTriangle, FileSpreadsheet, FileText, Shield, Wifi, Save, Smartphone } from 'lucide-react'
import { gdpr, fetchAPI, config as configApi, backups as backupsApi, downloadBlob, adminBundle, diagnostics } from '../../api'
import BackupRestore from './BackupRestore'
import DataRetentionSettings from './DataRetentionSettings'
import ReportScheduleManager from './ReportScheduleManager'
import ChargebackReport from './ChargebackReport'
import toast from 'react-hot-toast'
import { Button, Card, Input } from '../ui'

function downloadExport(endpoint, filename) {
  // endpoint comes in as '/api/...' — strip the /api prefix for downloadBlob
  const path = endpoint.startsWith('/api') ? endpoint.slice(4) : endpoint
  downloadBlob(path, filename).catch(() => {})
}

function NetworkTab() {
  const [hostIp, setHostIp] = useState('')
  const [detectedIp, setDetectedIp] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    configApi.getNetwork()
      .then(data => {
        setDetectedIp(data.detected_ip || '')
        // Show configured IP if available, otherwise show detected
        const configuredRow = data.configured_ip
        if (configuredRow) setHostIp(configuredRow)
        else if (data.detected_ip) setHostIp(data.detected_ip)
      })
      .catch(() => {})
    // Also fetch from system_config
    fetchAPI('/admin/oidc').catch(() => {})
  }, [])


  const handleSave = async () => {
    setError('')
    setSaving(true)
    setSaved(false)
    try {
      await configApi.saveNetwork({ host_ip: hostIp })
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card padding="lg" className="mb-4 md:mb-6">
      <div className="flex items-center gap-2 md:gap-3 mb-4">
        <Wifi size={18} className="text-[var(--brand-primary)]" />
        <h2 className="text-lg md:text-xl font-display font-semibold">Network & Camera Streaming</h2>
      </div>
      <p className="text-[var(--brand-text-muted)] text-sm mb-4">
        Configure the host IP address for WebRTC camera streaming. This should be the LAN IP of the server running O.D.I.N., reachable from your browser.
      </p>
      <div className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-[var(--brand-text-secondary)] mb-1.5">Host IP Address</label>
          <div className="flex gap-2">
            <Input
              type="text"
              value={hostIp}
              onChange={e => { setHostIp(e.target.value); setSaved(false) }}
              placeholder="e.g. 192.168.1.100"
              className="flex-1"
            />
            <Button
              variant="primary"
              icon={Save}
              loading={saving}
              disabled={!hostIp}
              onClick={handleSave}
            >
              {saved ? 'Saved!' : 'Save'}
            </Button>
          </div>
          {detectedIp && <p className="text-xs text-[var(--brand-text-muted)] mt-1">Auto-detected: {detectedIp} {detectedIp.startsWith('172.') && '(Docker internal — use your host LAN IP instead)'}</p>}
        </div>
        {error && (
          <div className="p-3 bg-red-900/30 border border-red-700/50 rounded-md text-sm text-red-300">
            {error}
          </div>
        )}
        <p className="text-xs text-[var(--brand-text-muted)]">
          Without this setting, camera streams may show a black screen. The IP is used for WebRTC ICE candidates so your browser can connect to the go2rtc video relay. Changes take effect immediately.
        </p>
      </div>
    </Card>
  )
}

function PrivacyDataCard() {
  const [exporting, setExporting] = useState(false)
  const [erasing, setErasing] = useState(false)
  const [confirmErase, setConfirmErase] = useState(false)
  const [message, setMessage] = useState(null)

  const getCurrentUserId = async () => {
    try {
      const me = await fetchAPI('/auth/me')
      return me?.id || null
    } catch {
      return null
    }
  }

  const handleExport = async () => {
    setExporting(true)
    setMessage(null)
    try {
      const userId = await getCurrentUserId()
      if (!userId) {
        setMessage({ type: 'error', text: 'Could not determine user ID' })
        return
      }
      const data = await gdpr.exportData(userId)
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `odin-my-data-${new Date().toISOString().slice(0, 10)}.json`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
      setMessage({ type: 'success', text: 'Data exported successfully' })
    } catch (err) {
      setMessage({ type: 'error', text: err.message || 'Export failed' })
    } finally {
      setExporting(false)
    }
  }

  const handleErase = async () => {
    setErasing(true)
    setMessage(null)
    try {
      const userId = await getCurrentUserId()
      if (!userId) {
        setMessage({ type: 'error', text: 'Could not determine user ID' })
        return
      }
      await gdpr.eraseData(userId)
      // Clear session — call logout to clear cookie
      await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {})
      localStorage.removeItem('odin_user')
      localStorage.removeItem('rbac_permissions')
      window.location.href = '/login'
    } catch (err) {
      setMessage({ type: 'error', text: err.message || 'Erase failed' })
    } finally {
      setErasing(false)
      setConfirmErase(false)
    }
  }

  return (
    <Card padding="lg" className="mb-4 md:mb-6">
      <div className="flex items-center gap-2 md:gap-3 mb-4">
        <Shield size={18} className="text-[var(--brand-primary)]" />
        <h2 className="text-lg md:text-xl font-display font-semibold">Privacy & Data</h2>
      </div>
      <p className="text-sm text-[var(--brand-text-secondary)] mb-4">
        Export or erase your personal data in compliance with GDPR. Exported data includes your profile, jobs, sessions, and preferences.
      </p>
      <div className="flex flex-wrap gap-3">
        <Button variant="primary" icon={Download} loading={exporting} onClick={handleExport}>
          Export My Data
        </Button>
        {confirmErase ? (
          <div className="flex items-center gap-2">
            <Button variant="danger" icon={Trash2} loading={erasing} onClick={handleErase}>
              Confirm Erase
            </Button>
            <Button variant="tertiary" onClick={() => setConfirmErase(false)}>
              Cancel
            </Button>
          </div>
        ) : (
          <Button variant="danger" icon={Trash2} onClick={() => setConfirmErase(true)} className="bg-red-900/50 hover:bg-red-800/50 text-red-300 border border-red-700/50">
            Erase My Data
          </Button>
        )}
      </div>
      {message && (
        <div className={`mt-3 text-sm px-3 py-2 rounded-md ${message.type === 'success' ? 'bg-green-900/30 text-green-400' : 'bg-red-900/30 text-red-400'}`}>
          {message.text}
        </div>
      )}
      <p className="text-xs text-[var(--brand-text-muted)] mt-3">
        Erasing your data is permanent. Your account will be anonymized and you will be logged out.
      </p>
    </Card>
  )
}

function InstallAppCard() {
  const [deferredPrompt, setDeferredPrompt] = useState(null)
  const [installed, setInstalled] = useState(false)

  useEffect(() => {
    const handler = (e) => { e.preventDefault(); setDeferredPrompt(e) }
    window.addEventListener('beforeinstallprompt', handler)
    window.addEventListener('appinstalled', () => setInstalled(true))
    // Check if already installed (standalone mode)
    if (window.matchMedia('(display-mode: standalone)').matches) setInstalled(true)
    return () => window.removeEventListener('beforeinstallprompt', handler)
  }, [])

  if (installed) {
    return (
      <div className="flex items-center gap-2 bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 md:p-6 mb-4 md:mb-6">
        <Smartphone size={18} className="text-green-400" />
        <span className="font-display font-semibold">App Installed</span>
        <span className="text-[var(--brand-text-muted)] text-sm ml-auto">Running as standalone app</span>
      </div>
    )
  }

  if (!deferredPrompt) return null

  return (
    <div className="flex items-center gap-2 bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 md:p-6 mb-4 md:mb-6">
      <Smartphone size={18} className="text-[var(--brand-primary)]" />
      <div>
        <span className="font-display font-semibold">Install App</span>
        <p className="text-[var(--brand-text-muted)] text-sm">Add O.D.I.N. to your home screen for quick access</p>
      </div>
      <Button
        variant="primary"
        onClick={() => { deferredPrompt.prompt(); deferredPrompt.userChoice.then(() => setDeferredPrompt(null)) }}
        className="ml-auto"
      >
        Install
      </Button>
    </div>
  )
}

function AuditLogLink() {
  return (
    <a href="/audit" className="flex items-center gap-2 bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 md:p-6 mb-4 md:mb-6 hover:bg-[var(--brand-input-bg)]/50 transition-colors">
      <FileText size={18} className="text-[var(--brand-primary)]" />
      <span className="font-display font-semibold">Audit Log</span>
      <span className="text-[var(--brand-text-muted)] text-sm ml-auto">View full audit log &rarr;</span>
    </a>
  )
}

export default function SystemTab() {
  const queryClient = useQueryClient()
  const [deleteConfirm, setDeleteConfirm] = useState(null)

  const { data: statsData } = useQuery({ queryKey: ['stats'], queryFn: () => fetchAPI('/stats') })
  const { data: backups, isLoading: backupsLoading } = useQuery({
    queryKey: ['backups'],
    queryFn: backupsApi.list
  })

  const formatSize = (bytes) => {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  const formatDate = (dateStr) => {
    const d = new Date(dateStr)
    return d.toLocaleDateString('en-US', {
      month: 'short', day: 'numeric', year: 'numeric',
      hour: '2-digit', minute: '2-digit'
    })
  }

  const createBackup = useMutation({
    mutationFn: backupsApi.create,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['backups'] })
      toast.success('Backup created')
    },
    onError: (err: any) => toast.error('Backup failed: ' + (err?.message || 'Unknown error')),
  })

  const deleteBackup = useMutation({
    mutationFn: backupsApi.remove,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['backups'] })
      setDeleteConfirm(null)
    },
    onError: (err: any) => toast.error('Delete backup failed: ' + (err?.message || 'Unknown error')),
  })

  return (
    <div className="max-w-4xl">
      {/* Database Info */}
      <Card padding="lg" className="mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <Database size={18} className="text-[var(--brand-primary)]" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Database</h2>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4">
          <div className="bg-[var(--brand-input-bg)] rounded-md p-3 md:p-4">
            <div className="text-xl md:text-2xl font-bold">{statsData?.printers?.total || 0}</div>
            <div className="text-xs md:text-sm text-[var(--brand-text-secondary)]">Printers</div>
          </div>
          <div className="bg-[var(--brand-input-bg)] rounded-md p-3 md:p-4">
            <div className="text-xl md:text-2xl font-bold">{statsData?.models || 0}</div>
            <div className="text-xs md:text-sm text-[var(--brand-text-secondary)]">Models</div>
          </div>
          <div className="bg-[var(--brand-input-bg)] rounded-md p-3 md:p-4">
            <div className="text-xl md:text-2xl font-bold">{(statsData?.jobs?.pending || 0) + (statsData?.jobs?.scheduled || 0) + (statsData?.jobs?.printing || 0)}</div>
            <div className="text-xs md:text-sm text-[var(--brand-text-secondary)]">Active Jobs</div>
          </div>
          <div className="bg-[var(--brand-input-bg)] rounded-md p-3 md:p-4">
            <div className="text-xl md:text-2xl font-bold">{statsData?.jobs?.completed || 0}</div>
            <div className="text-xs md:text-sm text-[var(--brand-text-secondary)]">Completed</div>
          </div>
        </div>
      </Card>

      {/* Support Bundle */}
      <Card padding="lg" className="mb-4 md:mb-6">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-semibold">Support Bundle</h3>
            <p className="text-xs text-[var(--brand-text-muted)] mt-1">Download a privacy-filtered diagnostic ZIP for issue reporting</p>
          </div>
          <Button
            variant="primary"
            size="md"
            icon={Download}
            onClick={async () => {
              try { await adminBundle.download(); toast.success('Bundle downloaded') }
              catch { toast.error('Failed to generate bundle') }
            }}
          >
            Download
          </Button>
        </div>
      </Card>

      {/* Diagnostics Report */}
      <Card padding="lg" className="mb-4 md:mb-6">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-semibold">Diagnostics Report</h3>
            <p className="text-xs text-[var(--brand-text-muted)] mt-1">Download a JSON diagnostic report for troubleshooting</p>
          </div>
          <Button
            variant="primary"
            size="md"
            icon={Download}
            onClick={async () => {
              try { await diagnostics.download(); toast.success('Diagnostics downloaded') }
              catch { toast.error('Failed to download diagnostics') }
            }}
          >
            Download
          </Button>
        </div>
      </Card>

      {/* Database Backups */}
      <Card padding="lg" className="mb-4 md:mb-6">
        <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
          <div className="flex items-center gap-2 md:gap-3">
            <HardDrive size={18} className="text-[var(--brand-primary)]" />
            <h2 className="text-lg md:text-xl font-display font-semibold">Database Backups</h2>
          </div>
          <Button variant="primary" icon={Plus} loading={createBackup.isPending} onClick={() => createBackup.mutate()}>
            Create Backup
          </Button>
        </div>

        {createBackup.isSuccess && (
          <div className="mb-4 p-3 bg-green-500/10 border border-green-500/30 rounded-md flex items-center gap-2">
            <CheckCircle size={16} className="text-green-400 flex-shrink-0" />
            <span className="text-green-200 text-sm">
              Backup created: {createBackup.data?.filename} ({formatSize(createBackup.data?.size_bytes || 0)})
            </span>
          </div>
        )}

        {createBackup.isError && (
          <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded-md flex items-center gap-2">
            <AlertTriangle size={16} className="text-red-400 flex-shrink-0" />
            <span className="text-red-200 text-sm">Failed to create backup</span>
          </div>
        )}

        {backupsLoading && (
          <div className="text-center text-[var(--brand-text-secondary)] py-6 text-sm">Loading backups...</div>
        )}

        {!backupsLoading && (!backups || backups.length === 0) && (
          <div className="text-center text-[var(--brand-text-muted)] py-6 text-sm">
            No backups yet. Create your first backup to protect your data.
          </div>
        )}

        {!backupsLoading && backups && backups.length > 0 && (
          <div className="space-y-2">
            {backups.map((backup) => (
              <div
                key={backup.filename}
                className="flex items-center justify-between p-3 bg-[var(--brand-input-bg)] rounded-md gap-3"
              >
                <div className="min-w-0 flex-1">
                  <div className="text-sm text-[var(--brand-text-primary)] font-mono truncate">{backup.filename}</div>
                  <div className="text-xs text-[var(--brand-text-secondary)] mt-0.5">
                    {formatSize(backup.size_bytes)} · {formatDate(backup.created_at)}
                  </div>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <Button
                    variant="tertiary"
                    size="icon"
                    icon={Download}
                    onClick={() => backupsApi.download(backup.filename)}
                    title="Download backup"
                  />
                  {deleteConfirm === backup.filename ? (
                    <div className="flex items-center gap-1">
                      <Button variant="danger" size="sm" loading={deleteBackup.isPending} onClick={() => deleteBackup.mutate(backup.filename)}>
                        Confirm
                      </Button>
                      <Button variant="tertiary" size="sm" onClick={() => setDeleteConfirm(null)}>
                        Cancel
                      </Button>
                    </div>
                  ) : (
                    <Button
                      variant="tertiary"
                      size="icon"
                      icon={Trash2}
                      onClick={() => setDeleteConfirm(backup.filename)}
                      title="Delete backup"
                      className="hover:bg-red-900 hover:text-red-400"
                    />
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        <p className="text-xs text-[var(--brand-text-muted)] mt-3">
          Backups use SQLite's online backup API — safe to create while the system is running.
        </p>
        <div className="border-t border-[var(--brand-card-border)] mt-4 pt-4">
          <BackupRestore />
        </div>
      </Card>

      {/* Data Export */}
      <Card padding="lg" className="mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <FileSpreadsheet size={18} className="text-[var(--brand-primary)]" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Data Export</h2>
        </div>
        <p className="text-sm text-[var(--brand-text-secondary)] mb-4">
          Export your data as CSV files for analysis, reporting, or backup purposes.
        </p>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Button variant="secondary" icon={Download} onClick={() => downloadExport('/api/export/jobs', 'jobs_export.csv')} fullWidth className="py-3">
            Jobs
          </Button>
          <Button variant="secondary" icon={Download} onClick={() => downloadExport('/api/export/models', 'models_export.csv')} fullWidth className="py-3">
            Models
          </Button>
          <Button variant="secondary" icon={Download} onClick={() => downloadExport('/api/export/spools', 'spools_export.csv')} fullWidth className="py-3">
            Spools
          </Button>
          <Button variant="secondary" icon={Download} onClick={() => downloadExport('/api/export/filament-usage', 'filament_usage_export.csv')} fullWidth className="py-3">
            Filament Usage
          </Button>
        </div>
      </Card>
      {/* Data Retention */}
      <Card padding="lg" className="mb-4 md:mb-6">
        <DataRetentionSettings />
      </Card>

      {/* Scheduled Reports */}
      <Card padding="lg" className="mb-4 md:mb-6">
        <ReportScheduleManager />
      </Card>

      {/* Cost Chargebacks */}
      <Card padding="lg" className="mb-4 md:mb-6">
        <ChargebackReport />
      </Card>

      <InstallAppCard />
      <AuditLogLink />

      {/* Privacy & Data (GDPR) */}
      <PrivacyDataCard />
    </div>
  )
}
