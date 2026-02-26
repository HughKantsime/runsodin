import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Database, HardDrive, Plus, RefreshCw, Download, Trash2, CheckCircle, AlertTriangle, FileSpreadsheet, FileText, Key, Shield, Wifi, Save, Smartphone } from 'lucide-react'
import { gdpr, fetchAPI, config as configApi, license as licenseApi, backups as backupsApi, downloadBlob, adminBundle } from '../../api'
import BackupRestore from './BackupRestore'
import DataRetentionSettings from './DataRetentionSettings'
import ReportScheduleManager from './ReportScheduleManager'
import ChargebackReport from './ChargebackReport'
import toast from 'react-hot-toast'

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
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
      <div className="flex items-center gap-2 md:gap-3 mb-4">
        <Wifi size={18} className="text-print-400" />
        <h2 className="text-lg md:text-xl font-display font-semibold">Network & Camera Streaming</h2>
      </div>
      <p className="text-farm-500 text-sm mb-4">
        Configure the host IP address for WebRTC camera streaming. This should be the LAN IP of the server running O.D.I.N., reachable from your browser.
      </p>
      <div className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-farm-300 mb-1.5">Host IP Address</label>
          <div className="flex gap-2">
            <input
              type="text"
              value={hostIp}
              onChange={e => { setHostIp(e.target.value); setSaved(false) }}
              placeholder="e.g. 192.168.1.100"
              className="flex-1 px-3 py-2 rounded-lg bg-farm-800 border border-farm-700 text-farm-100 placeholder-farm-600 focus:outline-none focus:ring-2 focus:ring-print-500/50 focus:border-print-500/50"
            />
            <button
              onClick={handleSave}
              disabled={!hostIp || saving}
              className="px-4 py-2 rounded-lg bg-print-600 hover:bg-print-500 text-white font-medium text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
            >
              {saving ? <RefreshCw size={14} className="animate-spin" /> : <Save size={14} />}
              {saved ? 'Saved!' : 'Save'}
            </button>
          </div>
          {detectedIp && <p className="text-xs text-farm-600 mt-1">Auto-detected: {detectedIp} {detectedIp.startsWith('172.') && '(Docker internal — use your host LAN IP instead)'}</p>}
        </div>
        {error && (
          <div className="p-3 bg-red-900/30 border border-red-700/50 rounded-lg text-sm text-red-300">
            {error}
          </div>
        )}
        <p className="text-xs text-farm-600">
          Without this setting, camera streams may show a black screen. The IP is used for WebRTC ICE candidates so your browser can connect to the go2rtc video relay. Changes take effect immediately.
        </p>
      </div>
    </div>
  )
}

function PrivacyDataCard() {
  const [exporting, setExporting] = useState(false)
  const [erasing, setErasing] = useState(false)
  const [confirmErase, setConfirmErase] = useState(false)
  const [message, setMessage] = useState(null)

  const getCurrentUserId = async () => {

    if (!token) return null
    try {
      const payload = JSON.parse(atob(token.split('.')[1]))
      const username = payload.sub
      const users = await fetchAPI('/users')
      const me = users.find(u => u.username === username)
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
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
      <div className="flex items-center gap-2 md:gap-3 mb-4">
        <Shield size={18} className="text-print-400" />
        <h2 className="text-lg md:text-xl font-display font-semibold">Privacy & Data</h2>
      </div>
      <p className="text-sm text-farm-400 mb-4">
        Export or erase your personal data in compliance with GDPR. Exported data includes your profile, jobs, sessions, and preferences.
      </p>
      <div className="flex flex-wrap gap-3">
        <button
          onClick={handleExport}
          disabled={exporting}
          className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
        >
          {exporting ? <RefreshCw size={16} className="animate-spin" /> : <Download size={16} />}
          Export My Data
        </button>
        {confirmErase ? (
          <div className="flex items-center gap-2">
            <button
              onClick={handleErase}
              disabled={erasing}
              className="flex items-center gap-2 px-4 py-2 bg-red-600 hover:bg-red-500 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
            >
              {erasing ? <RefreshCw size={16} className="animate-spin" /> : <Trash2 size={16} />}
              Confirm Erase
            </button>
            <button
              onClick={() => setConfirmErase(false)}
              className="px-4 py-2 bg-farm-700 hover:bg-farm-600 rounded-lg text-sm font-medium transition-colors"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            onClick={() => setConfirmErase(true)}
            className="flex items-center gap-2 px-4 py-2 bg-red-900/50 hover:bg-red-800/50 text-red-300 border border-red-700/50 rounded-lg text-sm font-medium transition-colors"
          >
            <Trash2 size={16} />
            Erase My Data
          </button>
        )}
      </div>
      {message && (
        <div className={`mt-3 text-sm px-3 py-2 rounded-lg ${message.type === 'success' ? 'bg-green-900/30 text-green-400' : 'bg-red-900/30 text-red-400'}`}>
          {message.text}
        </div>
      )}
      <p className="text-xs text-farm-500 mt-3">
        Erasing your data is permanent. Your account will be anonymized and you will be logged out.
      </p>
    </div>
  )
}

function LicenseTab() {
  const [licenseInfo, setLicenseInfo] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [activating, setActivating] = useState(false)
  const [licenseKey, setLicenseKey] = useState('')
  const [message, setMessage] = useState(null)
  const fileInputRef = useRef(null)

  useEffect(() => {
    loadLicense()
  }, [])

  const loadLicense = async () => {
    try {
      const data = await licenseApi.get()
      setLicenseInfo(data)
    } catch (e) { console.error('Failed to fetch license:', e) }
  }

  const handleActivate = async () => {
    if (!licenseKey.trim()) return
    setActivating(true)
    setMessage(null)
    try {
      const data = await licenseApi.activate(licenseKey.trim())
      setMessage({ type: 'success', text: `License activated: ${data.tier} tier` })
      setLicenseKey('')
      loadLicense()
    } catch (err) {
      setMessage({ type: 'error', text: 'Activation failed: ' + err.message })
    } finally {
      setActivating(false)
    }
  }

  const handleUpload = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    setMessage(null)
    try {
      const data = await licenseApi.upload(file)
      setMessage({ type: 'success', text: `License activated: ${data.tier} tier` })
      loadLicense()
    } catch (err) {
      setMessage({ type: 'error', text: 'Upload failed: ' + err.message })
    } finally {
      setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const handleRemove = async () => {
    if (!confirm('Remove license and revert to Community tier?')) return
    try {
      await licenseApi.remove()
      setMessage({ type: 'success', text: 'License removed. Reverted to Community tier.' })
      loadLicense()
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to remove license' })
    }
  }

  const handleExportActivationRequest = async () => {
    try {
      const data = await licenseApi.getActivationRequest()
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'odin-activation-request.json'
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to export activation request' })
    }
  }

  const copyInstallId = () => {
    if (licenseInfo?.installation_id) {
      navigator.clipboard.writeText(licenseInfo.installation_id)
      setMessage({ type: 'success', text: 'Installation ID copied to clipboard' })
    }
  }

  const tier = licenseInfo?.tier || 'community'
  const tierColors = {
    community: 'text-farm-400',
    pro: 'text-amber-400',
    education: 'text-blue-400',
    enterprise: 'text-purple-400',
  }

  return (
    <div className="space-y-6">
      <div className="rounded-lg p-5" style={{ backgroundColor: 'var(--brand-card-bg)', borderColor: 'var(--brand-sidebar-border)', border: '1px solid' }}>
        <div className="flex items-center gap-2 mb-4">
          <FileText size={18} className="text-print-400" />
          <h3 className="font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Current License</h3>
        </div>
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <span className="text-farm-500">Tier:</span>
            <span className={`ml-2 font-semibold capitalize ${tierColors[tier] || 'text-farm-300'}`}>{tier}</span>
          </div>
          <div>
            <span className="text-farm-500">License:</span>
            <span className="ml-2 text-farm-200">BSL 1.1 (converts to Apache 2.0 on 2029-02-07)</span>
          </div>
          {licenseInfo?.licensee && (
            <div>
              <span className="text-farm-500">Licensed to:</span>
              <span className="ml-2 text-farm-200">{licenseInfo.licensee}</span>
            </div>
          )}
          {licenseInfo?.expires && (
            <div>
              <span className="text-farm-500">Expires:</span>
              <span className="ml-2 text-farm-200">{new Date(licenseInfo.expires).toLocaleDateString()}</span>
            </div>
          )}
          <div>
            <span className="text-farm-500">Max printers:</span>
            <span className="ml-2 text-farm-200">{licenseInfo?.max_printers === -1 ? 'Unlimited' : (licenseInfo?.max_printers || 5)}</span>
          </div>
          <div>
            <span className="text-farm-500">Max users:</span>
            <span className="ml-2 text-farm-200">{licenseInfo?.max_users === -1 ? 'Unlimited' : (licenseInfo?.max_users || 1)}</span>
          </div>
        </div>

        {licenseInfo?.installation_id && (
          <div className="mt-4 pt-4 border-t border-farm-800">
            <span className="text-farm-500 text-sm">Installation ID:</span>
            <div className="flex items-center gap-2 mt-1">
              <code className="text-xs text-farm-300 bg-farm-900 px-2 py-1 rounded font-mono flex-1 select-all">{licenseInfo.installation_id}</code>
              <button onClick={copyInstallId} className="text-xs text-print-400 hover:text-print-300 transition-colors px-2 py-1">Copy</button>
            </div>
          </div>
        )}
      </div>

      <div className="rounded-lg p-5" style={{ backgroundColor: 'var(--brand-card-bg)', borderColor: 'var(--brand-sidebar-border)', border: '1px solid' }}>
        <div className="flex items-center gap-2 mb-4">
          <Key size={18} className="text-print-400" />
          <h3 className="font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Activate License</h3>
        </div>
        <p className="text-sm text-farm-400 mb-4">
          Enter your license key to activate online. The license will be bound to this installation.
        </p>
        <div className="flex gap-2">
          <input
            type="text"
            value={licenseKey}
            onChange={e => setLicenseKey(e.target.value)}
            placeholder="ODIN-XXXX-XXXX-XXXX"
            className="flex-1 px-3 py-2 rounded-lg bg-farm-900 border border-farm-700 text-farm-200 text-sm placeholder-farm-600 focus:outline-none focus:border-print-500"
            onKeyDown={e => e.key === 'Enter' && handleActivate()}
          />
          <button
            onClick={handleActivate}
            disabled={activating || !licenseKey.trim()}
            className="px-4 py-2 rounded-lg bg-print-600 text-white text-sm font-medium hover:bg-print-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {activating ? 'Activating...' : 'Activate'}
          </button>
        </div>

        <div className="mt-4 flex items-center gap-3">
          <button
            onClick={handleExportActivationRequest}
            className="text-xs text-farm-400 hover:text-print-400 transition-colors flex items-center gap-1"
          >
            <Download size={14} />
            Export activation request (offline)
          </button>
        </div>

        <div className="relative my-5">
          <div className="absolute inset-0 flex items-center">
            <div className="w-full border-t border-farm-700" />
          </div>
          <div className="relative flex justify-center text-xs">
            <span className="px-3 text-farm-500" style={{ backgroundColor: 'var(--brand-card-bg)' }}>or upload license file</span>
          </div>
        </div>

        <div className="flex gap-3">
          <label className="flex-1">
            <input
              ref={fileInputRef}
              type="file"
              accept=".license"
              onChange={handleUpload}
              className="hidden"
            />
            <div className={`w-full px-4 py-3 rounded-lg border-2 border-dashed text-center cursor-pointer transition-colors text-sm ${uploading ? 'border-farm-600 text-farm-500' : 'border-farm-700 text-farm-400 hover:border-print-500 hover:text-print-400'}`}>
              {uploading ? 'Uploading...' : 'Click to select .license file'}
            </div>
          </label>
        </div>
        {tier !== 'community' && (
          <button
            onClick={handleRemove}
            className="mt-3 text-xs text-red-400 hover:text-red-300 transition-colors"
          >
            Remove license (revert to Community)
          </button>
        )}
        {message && (
          <div className={`mt-3 text-sm px-3 py-2 rounded-lg ${message.type === 'success' ? 'bg-green-900/30 text-green-400' : 'bg-red-900/30 text-red-400'}`}>
            {message.text}
          </div>
        )}
      </div>
    </div>
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
      <div className="flex items-center gap-2 bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <Smartphone size={18} className="text-green-400" />
        <span className="font-display font-semibold">App Installed</span>
        <span className="text-farm-500 text-sm ml-auto">Running as standalone app</span>
      </div>
    )
  }

  if (!deferredPrompt) return null

  return (
    <div className="flex items-center gap-2 bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
      <Smartphone size={18} className="text-print-400" />
      <div>
        <span className="font-display font-semibold">Install App</span>
        <p className="text-farm-500 text-sm">Add O.D.I.N. to your home screen for quick access</p>
      </div>
      <button
        onClick={() => { deferredPrompt.prompt(); deferredPrompt.userChoice.then(() => setDeferredPrompt(null)) }}
        className="ml-auto px-4 py-2 bg-print-600 hover:bg-print-500 text-white rounded-lg text-sm font-medium transition-colors"
      >
        Install
      </button>
    </div>
  )
}

function AuditLogLink() {
  return (
    <a href="/audit" className="flex items-center gap-2 bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6 hover:bg-farm-800/50 transition-colors">
      <FileText size={18} className="text-print-400" />
      <span className="font-display font-semibold">Audit Log</span>
      <span className="text-farm-500 text-sm ml-auto">View full audit log &rarr;</span>
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
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['backups'] })
  })

  const deleteBackup = useMutation({
    mutationFn: backupsApi.remove,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['backups'] })
      setDeleteConfirm(null)
    }
  })

  return (
    <div className="max-w-4xl">
      {/* Database Info */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <Database size={18} className="text-print-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Database</h2>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4">
          <div className="bg-farm-800 rounded-lg p-3 md:p-4">
            <div className="text-xl md:text-2xl font-bold">{statsData?.printers?.total || 0}</div>
            <div className="text-xs md:text-sm text-farm-400">Printers</div>
          </div>
          <div className="bg-farm-800 rounded-lg p-3 md:p-4">
            <div className="text-xl md:text-2xl font-bold">{statsData?.models || 0}</div>
            <div className="text-xs md:text-sm text-farm-400">Models</div>
          </div>
          <div className="bg-farm-800 rounded-lg p-3 md:p-4">
            <div className="text-xl md:text-2xl font-bold">{(statsData?.jobs?.pending || 0) + (statsData?.jobs?.scheduled || 0) + (statsData?.jobs?.printing || 0)}</div>
            <div className="text-xs md:text-sm text-farm-400">Active Jobs</div>
          </div>
          <div className="bg-farm-800 rounded-lg p-3 md:p-4">
            <div className="text-xl md:text-2xl font-bold">{statsData?.jobs?.completed || 0}</div>
            <div className="text-xs md:text-sm text-farm-400">Completed</div>
          </div>
        </div>
      </div>

      {/* Support Bundle */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-sm font-semibold">Support Bundle</h3>
            <p className="text-xs text-farm-500 mt-1">Download a privacy-filtered diagnostic ZIP for issue reporting</p>
          </div>
          <button
            onClick={async () => {
              try { await adminBundle.download(); toast.success('Bundle downloaded') }
              catch { toast.error('Failed to generate bundle') }
            }}
            className="flex items-center gap-2 px-3 py-2 bg-print-600 hover:bg-print-700 rounded-lg text-sm transition-colors"
          >
            <Download size={14} /> Download
          </button>
        </div>
      </div>

      {/* Database Backups */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center justify-between mb-4 flex-wrap gap-3">
          <div className="flex items-center gap-2 md:gap-3">
            <HardDrive size={18} className="text-print-400" />
            <h2 className="text-lg md:text-xl font-display font-semibold">Database Backups</h2>
          </div>
          <button
            onClick={() => createBackup.mutate()}
            disabled={createBackup.isPending}
            className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 disabled:opacity-50 rounded-lg text-sm font-medium"
          >
            {createBackup.isPending ? (
              <RefreshCw size={16} className="animate-spin" />
            ) : (
              <Plus size={16} />
            )}
            Create Backup
          </button>
        </div>

        {createBackup.isSuccess && (
          <div className="mb-4 p-3 bg-green-500/10 border border-green-500/30 rounded-lg flex items-center gap-2">
            <CheckCircle size={16} className="text-green-400 flex-shrink-0" />
            <span className="text-green-200 text-sm">
              Backup created: {createBackup.data?.filename} ({formatSize(createBackup.data?.size_bytes || 0)})
            </span>
          </div>
        )}

        {createBackup.isError && (
          <div className="mb-4 p-3 bg-red-500/10 border border-red-500/30 rounded-lg flex items-center gap-2">
            <AlertTriangle size={16} className="text-red-400 flex-shrink-0" />
            <span className="text-red-200 text-sm">Failed to create backup</span>
          </div>
        )}

        {backupsLoading && (
          <div className="text-center text-farm-400 py-6 text-sm">Loading backups...</div>
        )}

        {!backupsLoading && (!backups || backups.length === 0) && (
          <div className="text-center text-farm-500 py-6 text-sm">
            No backups yet. Create your first backup to protect your data.
          </div>
        )}

        {!backupsLoading && backups && backups.length > 0 && (
          <div className="space-y-2">
            {backups.map((backup) => (
              <div
                key={backup.filename}
                className="flex items-center justify-between p-3 bg-farm-800 rounded-lg gap-3"
              >
                <div className="min-w-0 flex-1">
                  <div className="text-sm text-farm-100 font-mono truncate">{backup.filename}</div>
                  <div className="text-xs text-farm-400 mt-0.5">
                    {formatSize(backup.size_bytes)} · {formatDate(backup.created_at)}
                  </div>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <button
                    onClick={() => backupsApi.download(backup.filename)}
                    className="p-2 bg-farm-700 hover:bg-farm-600 rounded-lg text-farm-200 hover:text-white transition-colors"
                    title="Download backup"
                  >
                    <Download size={16} />
                  </button>
                  {deleteConfirm === backup.filename ? (
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => deleteBackup.mutate(backup.filename)}
                        disabled={deleteBackup.isPending}
                        className="px-2 py-1.5 bg-red-600 hover:bg-red-500 rounded-lg text-white text-xs font-medium"
                      >
                        Confirm
                      </button>
                      <button
                        onClick={() => setDeleteConfirm(null)}
                        className="px-2 py-1.5 bg-farm-700 hover:bg-farm-600 rounded-lg text-farm-200 text-xs"
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={() => setDeleteConfirm(backup.filename)}
                      className="p-2 bg-farm-700 hover:bg-red-900 rounded-lg text-farm-200 hover:text-red-400 transition-colors"
                      title="Delete backup"
                    >
                      <Trash2 size={16} />
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        <p className="text-xs text-farm-500 mt-3">
          Backups use SQLite's online backup API — safe to create while the system is running.
        </p>
        <div className="border-t border-farm-700 mt-4 pt-4">
          <BackupRestore />
        </div>
      </div>

      {/* Data Export */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <FileSpreadsheet size={18} className="text-print-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Data Export</h2>
        </div>
        <p className="text-sm text-farm-400 mb-4">
          Export your data as CSV files for analysis, reporting, or backup purposes.
        </p>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <button
            onClick={() => downloadExport('/api/export/jobs', 'jobs_export.csv')}
            className="flex items-center justify-center gap-2 px-4 py-3 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm font-medium transition-colors"
          >
            <Download size={16} />
            Jobs
          </button>
          <button
            onClick={() => downloadExport('/api/export/models', 'models_export.csv')}
            className="flex items-center justify-center gap-2 px-4 py-3 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm font-medium transition-colors"
          >
            <Download size={16} />
            Models
          </button>
          <button
            onClick={() => downloadExport('/api/export/spools', 'spools_export.csv')}
            className="flex items-center justify-center gap-2 px-4 py-3 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm font-medium transition-colors"
          >
            <Download size={16} />
            Spools
          </button>
          <button
            onClick={() => downloadExport('/api/export/filament-usage', 'filament_usage_export.csv')}
            className="flex items-center justify-center gap-2 px-4 py-3 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm font-medium transition-colors"
          >
            <Download size={16} />
            Filament Usage
          </button>
        </div>
      </div>
      {/* Data Retention */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <DataRetentionSettings />
      </div>

      {/* Scheduled Reports */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <ReportScheduleManager />
      </div>

      {/* Cost Chargebacks */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <ChargebackReport />
      </div>

      <InstallAppCard />
      <AuditLogLink />

      {/* Privacy & Data (GDPR) */}
      <PrivacyDataCard />

      {/* License — merged into System tab */}
      <div className="border-t border-farm-700 pt-6 mt-6">
        <LicenseTab />
      </div>
    </div>
  )
}
