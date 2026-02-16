import { useState, useEffect, useRef } from 'react'
import usePushNotifications from '../hooks/usePushNotifications'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Save, RefreshCw, Database, Clock, Plug, CheckCircle, XCircle, Download, Trash2, HardDrive, Plus, AlertTriangle, FileSpreadsheet, Bell, Mail, Smartphone, Settings as SettingsIcon, Users, Shield, Palette , Key, Webhook, FileText, Upload, Wifi, Eye, ChevronDown, ChevronRight, ExternalLink, Zap } from 'lucide-react'
import Admin from './Admin'
import Permissions from './Permissions'
import Branding from './Branding'
import OIDCSettings from '../components/OIDCSettings'
import WebhookSettings from '../components/WebhookSettings'
import GroupManager from '../components/GroupManager'

import MFASetup from '../components/MFASetup'
import APITokenManager from '../components/APITokenManager'
import SessionManager from '../components/SessionManager'
import QuotaManager from '../components/QuotaManager'
import IPAllowlistSettings from '../components/IPAllowlistSettings'
import DataRetentionSettings from '../components/DataRetentionSettings'
import BackupRestore from '../components/BackupRestore'
import OrgManager from '../components/OrgManager'
import ReportScheduleManager from '../components/ReportScheduleManager'
import ChargebackReport from '../components/ChargebackReport'
import { alertPreferences, smtpConfig, getEducationMode, setEducationMode, users as usersApi } from '../api'
import { getApprovalSetting, setApprovalSetting } from '../api'
import { useLicense } from '../LicenseContext'
import ProBadge from '../components/ProBadge'

const API_KEY = import.meta.env.VITE_API_KEY
const getApiHeaders = () => ({
  'Content-Type': 'application/json',
  'X-API-Key': API_KEY,
  'Authorization': `Bearer ${localStorage.getItem('token')}`
})

const backupsApi = {
  list: async () => {
    const res = await fetch('/api/backups', { headers: getApiHeaders() })
    if (!res.ok) throw new Error('Failed to list backups')
    return res.json()
  },
  create: async () => {
    const res = await fetch('/api/backups', { method: 'POST', headers: getApiHeaders() })
    if (!res.ok) throw new Error('Failed to create backup')
    return res.json()
  },
  remove: async (filename) => {
    const res = await fetch(`/api/backups/${filename}`, { method: 'DELETE', headers: getApiHeaders() })
    if (!res.ok) throw new Error('Failed to delete backup')
    return true
  },
  download: (filename) => {
    // Create a temporary link with the API key as a query param for download
    const a = document.createElement('a')
    a.href = `/api/backups/${filename}`
    a.download = filename
    // Use fetch + blob to include auth header
    return fetch(`/api/backups/${filename}`, { headers: { 'X-API-Key': API_KEY, 'Authorization': `Bearer ${localStorage.getItem('token')}` } })
      .then(res => res.blob())
      .then(blob => {
        const url = URL.createObjectURL(blob)
        a.href = url
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        URL.revokeObjectURL(url)
      })
  }
}


function ApprovalToggle() {
  const queryClient = useQueryClient()
  const { data: setting, isLoading } = useQuery({
    queryKey: ["approval-setting"],
    queryFn: getApprovalSetting,
  })

  const toggleMutation = useMutation({
    mutationFn: (enabled) => setApprovalSetting(enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["approval-setting"] }),
  })

  const enabled = setting?.require_job_approval || false

  return (
    <label className="flex items-center gap-3 cursor-pointer">
      <button
        type="button"
        role="switch"
        aria-checked={enabled}
        onClick={() => !isLoading && toggleMutation.mutate(!enabled)}
        className={`relative w-11 h-6 rounded-full transition-colors ${
          enabled ? "bg-print-600" : "bg-farm-700"
        }`}
      >
        <div className={`absolute top-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
          enabled ? "translate-x-[22px]" : "translate-x-0.5"
        }`} />
      </button>
      <span className="text-sm">
        {enabled ? "Approval required for viewer-role users" : "Approval disabled — all users create jobs directly"}
      </span>
    </label>
  )
}

function EducationModeToggle() {
  const queryClient = useQueryClient()
  const { data, isLoading } = useQuery({
    queryKey: ["education-mode"],
    queryFn: getEducationMode,
  })

  const toggleMutation = useMutation({
    mutationFn: (enabled) => setEducationMode(enabled),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["education-mode"] })
      // Notify sidebar to refresh
      window.dispatchEvent(new CustomEvent('education-mode-changed'))
    },
  })

  const enabled = data?.enabled || false

  return (
    <label className="flex items-center gap-3 cursor-pointer">
      <button
        type="button"
        role="switch"
        aria-checked={enabled}
        onClick={() => !isLoading && toggleMutation.mutate(!enabled)}
        className={`relative w-11 h-6 rounded-full transition-colors ${
          enabled ? "bg-blue-600" : "bg-farm-700"
        }`}
      >
        <div className={`absolute top-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
          enabled ? "translate-x-[22px]" : "translate-x-0.5"
        }`} />
      </button>
      <span className="text-sm">
        {enabled ? "Education mode enabled — Orders, Products hidden" : "Education mode disabled — all features visible"}
      </span>
    </label>
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

function NetworkTab() {
  const [hostIp, setHostIp] = useState('')
  const [detectedIp, setDetectedIp] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const headers = { 'X-API-Key': localStorage.getItem('api_key') || '', 'Authorization': `Bearer ${localStorage.getItem('token')}` }
    fetch('/api/setup/network', { headers })
      .then(r => r.json())
      .then(data => {
        setDetectedIp(data.detected_ip || '')
        // Show configured IP if available, otherwise show detected
        const configuredRow = data.configured_ip
        if (configuredRow) setHostIp(configuredRow)
        else if (data.detected_ip) setHostIp(data.detected_ip)
      })
      .catch(() => {})
    // Also fetch from system_config
    fetch('/api/admin/oidc', { headers }).catch(() => {})
  }, [])


  const handleSave = async () => {
    setError('')
    setSaving(true)
    setSaved(false)
    try {
      const headers = { 'Content-Type': 'application/json', 'X-API-Key': localStorage.getItem('api_key') || '', 'Authorization': `Bearer ${localStorage.getItem('token')}` }
      const resp = await fetch('/api/setup/network', { method: 'POST', headers, body: JSON.stringify({ host_ip: hostIp }) })
      if (!resp.ok) {
        const data = await resp.json()
        throw new Error(data.detail || 'Failed to save')
      }
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

function LicenseTab() {
  const [licenseInfo, setLicenseInfo] = useState(null)
  const [uploading, setUploading] = useState(false)
  const [message, setMessage] = useState(null)
  const fileInputRef = useRef(null)

  useEffect(() => {
    fetchLicense()
  }, [])

  const fetchLicense = async () => {
    try {
      const res = await fetch('/api/license', { headers: { 'X-API-Key': localStorage.getItem('api_key') || '', 'Authorization': `Bearer ${localStorage.getItem('token')}` } })
      if (res.ok) setLicenseInfo(await res.json())
    } catch (e) { console.error('Failed to fetch license:', e) }
  }

  const handleUpload = async (e) => {
    const file = e.target.files?.[0]
    if (!file) return
    setUploading(true)
    setMessage(null)
    try {
      const formData = new FormData()
      formData.append('file', file)
      const res = await fetch('/api/license/upload', {
        method: 'POST',
        headers: {
          'X-API-Key': localStorage.getItem('api_key') || '',
          'Authorization': `Bearer ${localStorage.getItem('token')}`,
        },
        body: formData,
      })
      const data = await res.json()
      if (res.ok) {
        setMessage({ type: 'success', text: `License activated: ${data.tier} tier` })
        fetchLicense()
      } else {
        setMessage({ type: 'error', text: data.detail || 'Upload failed' })
      }
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
      const res = await fetch('/api/license', {
        method: 'DELETE',
        headers: {
          'X-API-Key': localStorage.getItem('api_key') || '',
          'Authorization': `Bearer ${localStorage.getItem('token')}`,
        },
      })
      if (res.ok) {
        setMessage({ type: 'success', text: 'License removed. Reverted to Community tier.' })
        fetchLicense()
      }
    } catch (err) {
      setMessage({ type: 'error', text: 'Failed to remove license' })
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
      </div>

      <div className="rounded-lg p-5" style={{ backgroundColor: 'var(--brand-card-bg)', borderColor: 'var(--brand-sidebar-border)', border: '1px solid' }}>
        <div className="flex items-center gap-2 mb-4">
          <Upload size={18} className="text-print-400" />
          <h3 className="font-semibold" style={{ color: 'var(--brand-text-primary)' }}>Upload License</h3>
        </div>
        <p className="text-sm text-farm-400 mb-4">
          Upload an O.D.I.N. license file (.license) to unlock Pro, Education, or Enterprise features. License files are verified locally — no internet connection required.
        </p>
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

function VisionSettingsTab() {
  const [globalSettings, setGlobalSettings] = useState({ enabled: true, retention_days: 30 })
  const [printerSettings, setPrinterSettings] = useState([])
  const [models, setModels] = useState([])
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')
  const headers = getApiHeaders()

  useEffect(() => {
    // Load global settings
    fetch('/api/vision/settings', { headers }).then(r => r.json()).then(setGlobalSettings).catch(() => {})
    // Load models
    fetch('/api/vision/models', { headers }).then(r => r.json()).then(setModels).catch(() => {})
    // Load printers with vision settings
    fetch('/api/printers', { headers }).then(r => r.json()).then(async (printers) => {
      const withVision = await Promise.all(
        printers.filter(p => p.camera_url).map(async (p) => {
          try {
            const vs = await fetch(`/api/printers/${p.id}/vision`, { headers }).then(r => r.json())
            return { ...p, vision: vs }
          } catch { return { ...p, vision: null } }
        })
      )
      setPrinterSettings(withVision)
    }).catch(() => {})
  }, [])

  const saveGlobal = async () => {
    setSaving(true)
    try {
      await fetch('/api/vision/settings', { method: 'PATCH', headers, body: JSON.stringify(globalSettings) })
      setMsg('Settings saved')
      setTimeout(() => setMsg(''), 2000)
    } catch { setMsg('Save failed') }
    setSaving(false)
  }

  const savePrinterVision = async (printerId, data) => {
    try {
      await fetch(`/api/printers/${printerId}/vision`, { method: 'PATCH', headers, body: JSON.stringify(data) })
      setPrinterSettings(prev => prev.map(p =>
        p.id === printerId ? { ...p, vision: { ...p.vision, ...data } } : p
      ))
    } catch (e) { console.error('Failed to save printer vision settings:', e) }
  }

  const activateModel = async (modelId) => {
    try {
      const res = await fetch(`/api/vision/models/${modelId}/activate`, { method: 'PATCH', headers })
      if (res.ok) {
        const data = await res.json()
        setModels(prev => prev.map(m => ({
          ...m,
          is_active: m.detection_type === data.detection_type ? (m.id === modelId ? 1 : 0) : m.is_active
        })))
      }
    } catch (e) { console.error('Failed to activate model:', e) }
  }

  return (
    <div className="max-w-4xl space-y-6">
      {/* Global Settings */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6">
        <div className="flex items-center gap-2 mb-4">
          <Eye size={18} className="text-purple-400" />
          <h2 className="text-lg font-display font-semibold">Vigil AI</h2>
        </div>
        <p className="text-sm text-farm-400 mb-4">
          AI-powered print failure detection using local ONNX inference. No cloud services — all processing runs on this server.
        </p>

        <div className="space-y-4">
          <label className="flex items-center gap-3">
            <input
              type="checkbox"
              checked={globalSettings.enabled}
              onChange={e => setGlobalSettings(s => ({ ...s, enabled: e.target.checked }))}
              className="rounded-lg"
            />
            <span className="text-sm">Enable Vigil AI globally</span>
          </label>

          <div className="flex items-center gap-3">
            <label className="text-sm text-farm-400">Frame retention:</label>
            <input
              type="range" min="7" max="90" step="1"
              value={globalSettings.retention_days}
              onChange={e => setGlobalSettings(s => ({ ...s, retention_days: parseInt(e.target.value) }))}
              className="flex-1 max-w-xs"
            />
            <span className="text-sm w-16">{globalSettings.retention_days} days</span>
          </div>

          <div className="flex items-center gap-2">
            <button onClick={saveGlobal} disabled={saving} className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-sm font-medium transition-colors disabled:opacity-50">
              <Save size={14} className="inline mr-1.5" />
              Save
            </button>
            {msg && <span className="text-sm text-green-400">{msg}</span>}
          </div>
        </div>
      </div>

      {/* Per-Printer Settings */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6">
        <h3 className="text-base font-semibold mb-3">Per-Printer Detection Settings</h3>
        {printerSettings.length === 0 && (
          <p className="text-sm text-farm-500">No printers with cameras configured</p>
        )}
        <div className="space-y-3">
          {printerSettings.map(p => {
            const vs = p.vision || {}
            return (
              <div key={p.id} className="bg-farm-800 rounded-lg p-3">
                <div className="flex items-center justify-between mb-2">
                  <span className="font-medium text-sm">{p.nickname || p.name}</span>
                  <label className="flex items-center gap-2 text-xs">
                    <input
                      type="checkbox"
                      checked={vs.enabled !== 0}
                      onChange={e => savePrinterVision(p.id, { enabled: e.target.checked ? 1 : 0 })}
                    />
                    Enabled
                  </label>
                </div>
                {vs.enabled !== 0 && (
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                    {/* Spaghetti */}
                    <div>
                      <label className="flex items-center gap-1.5 mb-1">
                        <input type="checkbox" checked={vs.spaghetti_enabled !== 0}
                          onChange={e => savePrinterVision(p.id, { spaghetti_enabled: e.target.checked ? 1 : 0 })}
                        />
                        <span className="text-red-400">Spaghetti</span>
                      </label>
                      <input type="range" min="0.3" max="0.95" step="0.05"
                        value={vs.spaghetti_threshold || 0.65}
                        onChange={e => savePrinterVision(p.id, { spaghetti_threshold: parseFloat(e.target.value) })}
                        className="w-full"
                      />
                      <span className="text-farm-500">{((vs.spaghetti_threshold || 0.65) * 100).toFixed(0)}%</span>
                    </div>

                    {/* First Layer */}
                    <div>
                      <label className="flex items-center gap-1.5 mb-1">
                        <input type="checkbox" checked={vs.first_layer_enabled !== 0}
                          onChange={e => savePrinterVision(p.id, { first_layer_enabled: e.target.checked ? 1 : 0 })}
                        />
                        <span className="text-amber-400">First Layer</span>
                      </label>
                      <input type="range" min="0.3" max="0.95" step="0.05"
                        value={vs.first_layer_threshold || 0.60}
                        onChange={e => savePrinterVision(p.id, { first_layer_threshold: parseFloat(e.target.value) })}
                        className="w-full"
                      />
                      <span className="text-farm-500">{((vs.first_layer_threshold || 0.60) * 100).toFixed(0)}%</span>
                    </div>

                    {/* Detachment */}
                    <div>
                      <label className="flex items-center gap-1.5 mb-1">
                        <input type="checkbox" checked={vs.detachment_enabled !== 0}
                          onChange={e => savePrinterVision(p.id, { detachment_enabled: e.target.checked ? 1 : 0 })}
                        />
                        <span className="text-orange-400">Detachment</span>
                      </label>
                      <input type="range" min="0.3" max="0.95" step="0.05"
                        value={vs.detachment_threshold || 0.70}
                        onChange={e => savePrinterVision(p.id, { detachment_threshold: parseFloat(e.target.value) })}
                        className="w-full"
                      />
                      <span className="text-farm-500">{((vs.detachment_threshold || 0.70) * 100).toFixed(0)}%</span>
                    </div>

                    {/* Options */}
                    <div className="space-y-1.5">
                      <label className="flex items-center gap-1.5">
                        <input type="checkbox" checked={vs.auto_pause === 1}
                          onChange={e => savePrinterVision(p.id, { auto_pause: e.target.checked ? 1 : 0 })}
                        />
                        Auto-pause
                      </label>
                      <label className="flex items-center gap-1.5">
                        <input type="checkbox" checked={vs.collect_training_data === 1}
                          onChange={e => savePrinterVision(p.id, { collect_training_data: e.target.checked ? 1 : 0 })}
                        />
                        Collect data
                      </label>
                      <div className="flex items-center gap-1">
                        <span className="text-farm-500">Interval:</span>
                        <select
                          value={vs.capture_interval_sec || 10}
                          onChange={e => savePrinterVision(p.id, { capture_interval_sec: parseInt(e.target.value) })}
                          className="bg-farm-700 border border-farm-600 rounded-lg px-1 py-0.5 text-xs"
                        >
                          <option value={5}>5s</option>
                          <option value={10}>10s</option>
                          <option value={15}>15s</option>
                          <option value={30}>30s</option>
                          <option value={60}>60s</option>
                        </select>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>

      {/* Model Management */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6">
        <h3 className="text-base font-semibold mb-3">ONNX Models</h3>
        {models.length === 0 && (
          <p className="text-sm text-farm-500">No models uploaded. Upload ONNX models to enable detection.</p>
        )}
        <div className="space-y-2">
          {models.map(m => (
            <div key={m.id} className="flex items-center justify-between py-2 px-3 bg-farm-800 rounded-lg">
              <div className="flex items-center gap-3">
                <span className="text-sm font-medium">{m.name}</span>
                <span className="text-xs text-farm-500">{m.detection_type}</span>
                {m.version && <span className="text-xs text-farm-600">v{m.version}</span>}
              </div>
              <div className="flex items-center gap-2">
                {m.is_active ? (
                  <span className="flex items-center gap-1 text-xs text-green-400">
                    <CheckCircle size={12} />
                    Active
                  </span>
                ) : (
                  <button
                    onClick={() => activateModel(m.id)}
                    className="text-xs text-farm-400 hover:text-white px-2 py-1 bg-farm-700 rounded-lg transition-colors"
                  >
                    Activate
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>

        {/* Upload */}
        <div className="mt-3">
          <ModelUpload headers={headers} onUploaded={(m) => setModels(prev => [m, ...prev])} />
        </div>
      </div>
    </div>
  )
}

function ModelUpload({ headers, onUploaded }) {
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef(null)

  const handleUpload = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) return
    setUploading(true)
    try {
      const formData = new FormData()
      formData.append('file', file)
      const name = file.name.replace('.onnx', '')
      const dt = name.includes('spaghetti') ? 'spaghetti' : name.includes('first') ? 'first_layer' : name.includes('detach') ? 'detachment' : 'spaghetti'
      const res = await fetch(`/api/vision/models?name=${encodeURIComponent(name)}&detection_type=${dt}&input_size=640`, {
        method: 'POST',
        headers: { 'X-API-Key': headers['X-API-Key'], 'Authorization': headers['Authorization'] },
        body: formData,
      })
      if (res.ok) {
        const data = await res.json()
        onUploaded(data)
        fileRef.current.value = ''
      }
    } catch (e) { console.error('Upload failed:', e) }
    setUploading(false)
  }

  return (
    <div className="flex items-center gap-2">
      <input ref={fileRef} type="file" accept=".onnx" className="text-xs text-farm-400" />
      <button
        onClick={handleUpload}
        disabled={uploading}
        className="flex items-center gap-1.5 px-3 py-1.5 bg-farm-700 hover:bg-farm-600 rounded-lg text-xs font-medium transition-colors disabled:opacity-50"
      >
        <Upload size={12} />
        {uploading ? 'Uploading...' : 'Upload Model'}
      </button>
    </div>
  )
}

function AccessAccordion({ title, icon: Icon, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="border border-farm-800 rounded-lg overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-4 py-3 bg-farm-900 hover:bg-farm-800/70 transition-colors text-left"
      >
        {Icon && <Icon size={16} className="text-print-400" />}
        <span className="font-medium text-sm">{title}</span>
        {open ? <ChevronDown size={14} className="ml-auto text-farm-500" /> : <ChevronRight size={14} className="ml-auto text-farm-500" />}
      </button>
      {open && <div className="p-4 md:p-6 border-t border-farm-800">{children}</div>}
    </div>
  )
}

function downloadExport(endpoint, filename) {
  const token = localStorage.getItem('token')
  const apiKey = import.meta.env.VITE_API_KEY
  const headers = {}
  if (token) headers['Authorization'] = `Bearer ${token}`
  if (apiKey) headers['X-API-Key'] = apiKey
  fetch(endpoint, { headers })
    .then(res => {
      if (!res.ok) throw new Error('Export failed')
      return res.blob()
    })
    .then(blob => {
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    })
    .catch(() => {})
}

export default function Settings() {
  const queryClient = useQueryClient()
  const [settings, setSettings] = useState({
    spoolman_url: '',
    blackout_start: '22:30',
    blackout_end: '05:30',
  })
  const [saved, setSaved] = useState(false)
  const [deleteConfirm, setDeleteConfirm] = useState(null)
  const [activeTab, setActiveTab] = useState('general')
  const lic = useLicense()
  const [alertPrefs, setAlertPrefs] = useState([])
  const [alertPrefsLoading, setAlertPrefsLoading] = useState(true)
  const [alertPrefsSaved, setAlertPrefsSaved] = useState(false)
  const [alertPrefsError, setAlertPrefsError] = useState(null)
  const { isSupported: pushSupported, isSubscribed: pushSubscribed, permission: pushPermission, 
          loading: pushLoading, subscribe: subscribePush, unsubscribe: unsubscribePush } = usePushNotifications()

  // Load alert preferences
  useEffect(() => {
    const loadAlertPrefs = async () => {
      try {
        const data = await alertPreferences.get()
        // Ensure we have all 4 types
        const types = ['print_complete', 'print_failed', 'spool_low', 'maintenance_overdue']
        const prefs = types.map(type => {
          const existing = data.find(p => p.alert_type === type || p.alert_type === type.toUpperCase())
          return existing || { alert_type: type, in_app: true, browser_push: false, email: false, threshold_value: type === 'spool_low' ? 100 : null }
        })
        setAlertPrefs(prefs)
      } catch (err) {
        console.error('Failed to load alert preferences:', err)
        // Set defaults if no prefs exist
        setAlertPrefs([
          { alert_type: 'print_complete', in_app: true, browser_push: false, email: false, threshold_value: null },
          { alert_type: 'print_failed', in_app: true, browser_push: true, email: false, threshold_value: null },
          { alert_type: 'spool_low', in_app: true, browser_push: false, email: false, threshold_value: 100 },
          { alert_type: 'maintenance_overdue', in_app: true, browser_push: false, email: false, threshold_value: null },
        ])
      } finally {
        setAlertPrefsLoading(false)
      }
    }
    loadAlertPrefs()
  }, [])

  const toggleAlertPref = (alertType, field) => {
    setAlertPrefs(prev => prev.map(p => 
      (p.alert_type === alertType || p.alert_type === alertType.toUpperCase())
        ? { ...p, [field]: !p[field] }
        : p
    ))
  }

  const setThreshold = (alertType, value) => {
    setAlertPrefs(prev => prev.map(p =>
      (p.alert_type === alertType || p.alert_type === alertType.toUpperCase())
        ? { ...p, threshold_value: value }
        : p
    ))
  }

  const saveAlertPrefs = async () => {
    try {
      setAlertPrefsError(null)
      await alertPreferences.update(alertPrefs)
      setAlertPrefsSaved(true)
      setTimeout(() => setAlertPrefsSaved(false), 3000)
    } catch (err) {
      setAlertPrefsError('Failed to save alert preferences')
      console.error(err)
    }
  }

  const alertTypeLabels = {
    'print_complete': { label: 'Print Complete', desc: 'When a print job finishes successfully', icon: '✅' },
    'print_failed': { label: 'Print Failed', desc: 'When a print fails or errors out', icon: '❌' },
    'spool_low': { label: 'Spool Low', desc: 'When filament drops below threshold', icon: '🟡' },
    'maintenance_overdue': { label: 'Maintenance Due', desc: 'When printer maintenance is overdue', icon: '🔧' },
    'job_submitted': { label: 'Job Submitted', desc: 'When a user submits a job for approval', icon: '📋' },
    'job_approved': { label: 'Job Approved', desc: 'When a submitted job is approved', icon: '👍' },
    'job_rejected': { label: 'Job Rejected', desc: 'When a submitted job is rejected', icon: '👎' },
  }

  const normalizeType = (type) => type.toLowerCase()

  // SMTP config state
  const [smtp, setSmtp] = useState({ enabled: false, host: '', port: 587, username: '', password: '', from_address: '' })
  const [smtpLoading, setSmtpLoading] = useState(true)
  const [smtpSaved, setSmtpSaved] = useState(false)
  const [smtpError, setSmtpError] = useState(null)
  const [smtpTesting, setSmtpTesting] = useState(false)
  const [uiMode, setUiMode] = useState('advanced')
  const [smtpTestResult, setSmtpTestResult] = useState(null)
  const [showSmtpPassword, setShowSmtpPassword] = useState(false)

  useEffect(() => {
    const loadSmtp = async () => {
      try {
        const data = await smtpConfig.get()
        setSmtp({
          enabled: data.enabled || false,
          host: data.host || '',
          port: data.port || 587,
          username: data.username || '',
          password: '',
          from_address: data.from_address || '',
          _password_set: data.password_set || false,
        })
      } catch (err) {
        console.error('Failed to load SMTP config:', err)
      } finally {
        setSmtpLoading(false)
      }
    }
    loadSmtp()
  }, [])

  const saveSmtp = async () => {
    try {
      setSmtpError(null)
      const payload = { ...smtp }
      // Don't send empty password if one is already set (preserve existing)
      if (!payload.password && payload._password_set) {
        delete payload.password
      }
      delete payload._password_set
      await smtpConfig.update(payload)
      setSmtpSaved(true)
      setSmtp(s => ({ ...s, _password_set: true }))
      setTimeout(() => setSmtpSaved(false), 3000)
    } catch (err) {
      setSmtpError('Failed to save SMTP configuration')
      console.error(err)
    }
  }

  const testEmail = async () => {
    try {
      setSmtpTesting(true)
      setSmtpTestResult(null)
      const result = await smtpConfig.testEmail()
      setSmtpTestResult({ success: true, message: result.message || 'Test email sent!' })
    } catch (err) {
      setSmtpTestResult({ success: false, message: err.message || 'Failed to send test email' })
    } finally {
      setSmtpTesting(false)
    }
  }

  const { data: statsData } = useQuery({ queryKey: ['stats'], queryFn: () => fetch('/api/stats').then(r => r.json()) })
  const { data: configData } = useQuery({ queryKey: ['config'], queryFn: () => fetch('/api/config').then(r => r.json()) })
  const { data: usersData } = useQuery({ queryKey: ['users-count'], queryFn: usersApi.list, enabled: !lic.isPro })
  const { data: backups, isLoading: backupsLoading } = useQuery({
    queryKey: ['backups'],
    queryFn: backupsApi.list
  })

  useEffect(() => {
    if (configData) {
      setSettings({
        spoolman_url: configData.spoolman_url || '',
        blackout_start: configData.blackout_start || '22:30',
        blackout_end: configData.blackout_end || '05:30',
      })
    }
  }, [configData])

  // Fetch UI mode
  useEffect(() => {
    fetch('/api/pricing-config', { headers: { 'X-API-Key': API_KEY, 'Authorization': `Bearer ${localStorage.getItem('token')}` } })
      .then(r => r.json()).then(d => { if (d.ui_mode) setUiMode(d.ui_mode) }).catch(() => {})
  }, [])
  const toggleUiMode = async (mode) => {
    setUiMode(mode)
    try {
      const current = await fetch('/api/pricing-config', { headers: { 'X-API-Key': API_KEY, 'Authorization': `Bearer ${localStorage.getItem('token')}` } }).then(r => r.json())
      await fetch('/api/pricing-config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY, 'Authorization': `Bearer ${localStorage.getItem('token')}` },
        body: JSON.stringify({ ...current, ui_mode: mode })
      })
      window.dispatchEvent(new CustomEvent('ui-mode-changed', { detail: mode }))
    } catch (e) { console.error('Failed to save UI mode:', e) }
  }
  const saveSettings = useMutation({
    mutationFn: (data) => fetch('/api/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['config'] }); queryClient.invalidateQueries({ queryKey: ['stats'] }); setSaved(true); setTimeout(() => setSaved(false), 3000) },
  })

  const testSpoolman = useMutation({ mutationFn: () => fetch('/api/spoolman/test').then(r => r.json()) })

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

  const handleSave = () => saveSettings.mutate(settings)

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

  const PRO_TABS = ['access', 'integrations', 'branding']
  const ALL_TABS = [
    { id: 'general', label: 'General', icon: SettingsIcon },
    { id: 'notifications', label: 'Notifications', icon: Bell },
    { id: 'access', label: 'Access', icon: Users },
    { id: 'integrations', label: 'Integrations', icon: Webhook },
    ...(uiMode === 'advanced' ? [{ id: 'vision', label: 'Vigil AI', icon: Eye }] : []),
    { id: 'branding', label: 'Branding', icon: Palette },
    { id: 'system', label: 'System', icon: Database },
  ]
  const TABS = ALL_TABS.map(t => ({
    ...t,
    disabled: !lic.isPro && PRO_TABS.includes(t.id),
  }))

  return (
    <div className="p-4 md:p-6">
      <div className="mb-4 md:mb-6">
        <h1 className="text-2xl md:text-3xl font-display font-bold">Settings</h1>
        <p className="text-farm-500 text-sm mt-1">Configure your print farm</p>
      </div>

      {/* Tab Bar */}
      <div className="flex gap-1 mb-6 overflow-x-auto pb-1 -mx-1 px-1">
        {TABS.map(tab => (
          <button
            key={tab.id}
            onClick={() => !tab.disabled && setActiveTab(tab.id)}
            disabled={tab.disabled}
            className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors ${
              tab.disabled
                ? 'bg-farm-900/50 text-farm-600 cursor-not-allowed'
                : activeTab === tab.id
                  ? 'bg-print-600 text-white'
                  : 'bg-farm-900 text-farm-400 hover:bg-farm-800 hover:text-farm-200'
            }`}
          >
            <tab.icon size={16} />
            <span className="hidden sm:inline">{tab.label}</span>
            {tab.disabled && <ProBadge />}
          </button>
        ))}
      </div>

      {/* ==================== ACCESS TAB (Users + Permissions + SSO + MFA) ==================== */}
      {activeTab === 'access' && <div className="max-w-4xl space-y-3">
        <AccessAccordion title="Users & Groups" icon={Users} defaultOpen>
          <Admin />
          <div className="border-t border-farm-700 pt-6 mt-6">
            <GroupManager />
          </div>
        </AccessAccordion>
        <AccessAccordion title="Permissions" icon={Shield}>
          <Permissions />
        </AccessAccordion>
        <AccessAccordion title="Authentication (OIDC & MFA)" icon={Key}>
          <OIDCSettings />
          <div className="border-t border-farm-700 pt-6 mt-6">
            <MFASetup />
          </div>
        </AccessAccordion>
        <AccessAccordion title="Tokens & Sessions" icon={Key}>
          <APITokenManager />
          <div className="border-t border-farm-700 pt-6 mt-6">
            <SessionManager />
          </div>
        </AccessAccordion>
        <AccessAccordion title="Quotas & Restrictions" icon={Shield}>
          <QuotaManager />
          <div className="border-t border-farm-700 pt-6 mt-6">
            <IPAllowlistSettings />
          </div>
        </AccessAccordion>
        <AccessAccordion title="Organizations" icon={Users}>
          <OrgManager />
        </AccessAccordion>
      </div>}

      {/* ==================== INTEGRATIONS TAB (Webhooks) ==================== */}
      {activeTab === 'integrations' && <div className="max-w-4xl">
        <WebhookSettings />
      </div>}

      {/* ==================== BRANDING TAB ==================== */}
      {activeTab === 'branding' && <Branding />}

      {/* ==================== NOTIFICATIONS TAB (Alerts + Email) ==================== */}
      {activeTab === 'notifications' && <div className="max-w-4xl">
          {/* Push Notification Status */}
          {pushSupported && (
            <div className="mb-6 p-4 bg-farm-800 rounded-lg">
              <div className="flex items-center justify-between">
                <div>
                  <h4 className="font-medium">Browser Push Notifications</h4>
                  <p className="text-sm text-farm-400 mt-1">
                    {pushSubscribed 
                      ? 'Push notifications are enabled for this browser'
                      : pushPermission === 'denied'
                        ? 'Push notifications are blocked. Enable in browser settings.'
                        : 'Enable to receive alerts even when the tab is closed'}
                  </p>
                </div>
                <button
                  onClick={() => pushSubscribed ? unsubscribePush() : subscribePush()}
                  disabled={pushLoading || pushPermission === 'denied'}
                  className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                    pushSubscribed
                      ? 'bg-farm-700 hover:bg-farm-600 text-white'
                      : 'bg-print-600 hover:bg-print-500 text-white'
                  } disabled:opacity-50`}
                >
                  {pushLoading ? 'Loading...' : pushSubscribed ? 'Disable Push' : 'Enable Push'}
                </button>
              </div>
            </div>
          )}


      {/* Alert Preferences */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <Bell size={18} className="text-print-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Alert Preferences</h2>
        </div>
        <p className="text-sm text-farm-400 mb-4">
          Choose how you want to be notified for each type of alert.
        </p>

        {alertPrefsLoading ? (
          <div className="text-center text-farm-400 py-6 text-sm">Loading preferences...</div>
        ) : (
          <>
            {/* Header row */}
            <div className="hidden md:grid md:grid-cols-[1fr,80px,80px,80px] gap-3 mb-2 px-3">
              <div className="text-xs text-farm-500 uppercase tracking-wider">Alert Type</div>
              <div className="text-xs text-farm-500 uppercase tracking-wider text-center">In-App</div>
              <div className="text-xs text-farm-500 uppercase tracking-wider text-center">Push</div>
              <div className="text-xs text-farm-500 uppercase tracking-wider text-center">Email</div>
            </div>

            <div className="space-y-2">
              {alertPrefs.map((pref) => {
                const typeKey = normalizeType(pref.alert_type)
                const meta = alertTypeLabels[typeKey] || { label: typeKey, desc: '', icon: '🔔' }
                return (
                  <div key={typeKey} className="bg-farm-800 rounded-lg p-3">
                    <div className="md:grid md:grid-cols-[1fr,80px,80px,80px] gap-3 items-center">
                      {/* Label */}
                      <div>
                        <div className="flex items-center gap-2">
                          <span>{meta.icon}</span>
                          <span className="text-sm font-medium">{meta.label}</span>
                        </div>
                        <p className="text-xs text-farm-500 mt-0.5 ml-6 md:ml-0">{meta.desc}</p>
                      </div>

                      {/* Toggles */}
                      <div className="flex md:justify-center items-center gap-4 md:gap-0 mt-2 md:mt-0 ml-6 md:ml-0">
                        <span className="text-xs text-farm-500 md:hidden mr-1">In-App</span>
                        <button
                          type="button"
                          role="switch"
                          aria-checked={pref.in_app}
                          onClick={() => toggleAlertPref(pref.alert_type, 'in_app')}
                          className={`w-10 h-5 rounded-full transition-colors relative ${pref.in_app ? 'bg-print-600' : 'bg-farm-600'}`}
                        >
                          <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${pref.in_app ? 'left-5' : 'left-0.5'}`} />
                        </button>
                      </div>

                      <div className="flex md:justify-center items-center gap-4 md:gap-0 mt-1 md:mt-0 ml-6 md:ml-0">
                        <span className="text-xs text-farm-500 md:hidden mr-1">Push</span>
                        <button
                          type="button"
                          role="switch"
                          aria-checked={pref.browser_push}
                          onClick={() => toggleAlertPref(pref.alert_type, 'browser_push')}
                          className={`w-10 h-5 rounded-full transition-colors relative ${pref.browser_push ? 'bg-print-600' : 'bg-farm-600'}`}
                          title="Requires browser push setup"
                        >
                          <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${pref.browser_push ? 'left-5' : 'left-0.5'}`} />
                        </button>
                      </div>

                      <div className="flex md:justify-center items-center gap-4 md:gap-0 mt-1 md:mt-0 ml-6 md:ml-0">
                        <span className="text-xs text-farm-500 md:hidden mr-1">Email</span>
                        <button
                          type="button"
                          role="switch"
                          aria-checked={pref.email}
                          onClick={() => toggleAlertPref(pref.alert_type, 'email')}
                          className={`w-10 h-5 rounded-full transition-colors relative ${pref.email ? 'bg-print-600' : 'bg-farm-600'}`}
                          title="Requires SMTP configuration"
                        >
                          <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${pref.email ? 'left-5' : 'left-0.5'}`} />
                        </button>
                      </div>
                    </div>

                    {/* Threshold for spool_low */}
                    {typeKey === 'spool_low' && (
                      <div className="mt-3 pt-3 border-t border-farm-700 ml-6 md:ml-0">
                        <div className="flex items-center gap-3">
                          <label className="text-xs text-farm-400 whitespace-nowrap">Low spool threshold:</label>
                          <input
                            type="range"
                            min="50"
                            max="500"
                            step="25"
                            value={pref.threshold_value || 100}
                            onChange={(e) => setThreshold(pref.alert_type, parseInt(e.target.value))}
                            className="flex-1 accent-print-500 max-w-[200px]"
                          />
                          <span className="text-sm font-mono text-print-400 w-14 text-right">{pref.threshold_value || 100}g</span>
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>

            <div className="flex items-center gap-4 mt-4">
              <button
                onClick={saveAlertPrefs}
                className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-sm font-medium"
              >
                <Save size={16} />
                Save Preferences
              </button>
              {alertPrefsSaved && (
                <span className="flex items-center gap-1 text-green-400 text-sm">
                  <CheckCircle size={14} /> Preferences saved!
                </span>
              )}
              {alertPrefsError && (
                <span className="flex items-center gap-1 text-red-400 text-sm">
                  <AlertTriangle size={14} /> {alertPrefsError}
                </span>
              )}
            </div>

            <p className="text-xs text-farm-500 mt-3">
              Push notifications require browser permission. Email notifications require SMTP configuration below.
            </p>
          </>
        )}
      </div>

      {/* SMTP Email Configuration — merged into Notifications tab */}
      {lic.isPro && <>
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <Mail size={18} className="text-print-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Email Notifications (SMTP)</h2>
          <button
            type="button"
            role="switch"
            aria-checked={smtp.enabled}
            onClick={() => setSmtp(s => ({ ...s, enabled: !s.enabled }))}
            className={`ml-auto w-10 h-5 rounded-full transition-colors relative ${smtp.enabled ? 'bg-print-600' : 'bg-farm-600'}`}
          >
            <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${smtp.enabled ? 'left-5' : 'left-0.5'}`} />
          </button>
        </div>

        {smtpLoading ? (
          <div className="text-center text-farm-400 py-6 text-sm">Loading...</div>
        ) : (
          <>
            <p className="text-sm text-farm-400 mb-4">
              Configure an SMTP server to receive alert notifications via email.
              {!smtp.enabled && ' Enable the toggle above to activate email delivery.'}
            </p>

            <div className={`space-y-3 ${!smtp.enabled ? 'opacity-50 pointer-events-none' : ''}`}>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm text-farm-400 mb-1">SMTP Host</label>
                  <input
                    type="text"
                    value={smtp.host}
                    onChange={(e) => setSmtp(s => ({ ...s, host: e.target.value }))}
                    placeholder="smtp.gmail.com"
                    className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label className="block text-sm text-farm-400 mb-1">Port</label>
                  <input
                    type="number"
                    value={smtp.port}
                    onChange={(e) => setSmtp(s => ({ ...s, port: parseInt(e.target.value) || 587 }))}
                    className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm text-farm-400 mb-1">Username</label>
                  <input
                    type="text"
                    value={smtp.username}
                    onChange={(e) => setSmtp(s => ({ ...s, username: e.target.value }))}
                    placeholder="your@email.com"
                    className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
                  />
                </div>
                <div>
                  <label className="block text-sm text-farm-400 mb-1">Password</label>
                  <div className="relative">
                    <input
                      type={showSmtpPassword ? 'text' : 'password'}
                      value={smtp.password}
                      onChange={(e) => setSmtp(s => ({ ...s, password: e.target.value }))}
                      placeholder={smtp._password_set ? '••••••••  (saved)' : 'App password or SMTP password'}
                      className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm pr-16"
                    />
                    <button
                      type="button"
                      onClick={() => setShowSmtpPassword(!showSmtpPassword)}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-farm-400 hover:text-farm-200"
                    >
                      {showSmtpPassword ? 'Hide' : 'Show'}
                    </button>
                  </div>
                </div>
              </div>

              <div>
                <label className="block text-sm text-farm-400 mb-1">From Address</label>
                <input
                  type="text"
                  value={smtp.from_address}
                  onChange={(e) => setSmtp(s => ({ ...s, from_address: e.target.value }))}
                  placeholder="odin@yourdomain.com"
                  className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm max-w-md"
                />
              </div>
            </div>

            <div className="flex items-center gap-3 mt-4 flex-wrap">
              <button
                onClick={saveSmtp}
                className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-sm font-medium"
              >
                <Save size={16} />
                Save SMTP
              </button>
              <button
                onClick={testEmail}
                disabled={smtpTesting || !smtp.enabled || !smtp.host}
                className="flex items-center gap-2 px-4 py-2 bg-farm-700 hover:bg-farm-600 disabled:opacity-50 rounded-lg text-sm"
              >
                {smtpTesting ? <RefreshCw size={16} className="animate-spin" /> : <Mail size={16} />}
                Send Test Email
              </button>
              {smtpSaved && (
                <span className="flex items-center gap-1 text-green-400 text-sm">
                  <CheckCircle size={14} /> SMTP saved!
                </span>
              )}
              {smtpError && (
                <span className="flex items-center gap-1 text-red-400 text-sm">
                  <AlertTriangle size={14} /> {smtpError}
                </span>
              )}
            </div>

            {smtpTestResult && (
              <div className={`mt-3 p-3 rounded-lg flex items-center gap-2 ${smtpTestResult.success ? 'bg-green-500/10 border border-green-500/30' : 'bg-red-500/10 border border-red-500/30'}`}>
                {smtpTestResult.success ? <CheckCircle size={16} className="text-green-400 flex-shrink-0" /> : <XCircle size={16} className="text-red-400 flex-shrink-0" />}
                <span className={`text-sm ${smtpTestResult.success ? 'text-green-200' : 'text-red-200'}`}>{smtpTestResult.message}</span>
              </div>
            )}

            <p className="text-xs text-farm-500 mt-3">
              For Gmail, use an App Password (not your regular password). Go to Google Account → Security → App passwords.
            </p>
          </>
        )}
      </div>

      </>}

      </div>}

      {/* ==================== GENERAL TAB ==================== */}
      {activeTab === 'general' && <div className="max-w-4xl">

      {/* Upgrade Card (Community only) */}
      {!lic.isPro && (
        <div className="bg-amber-900/20 rounded-lg border border-amber-700/30 p-4 md:p-6 mb-4 md:mb-6">
          <div className="flex items-center gap-2 mb-3">
            <Zap size={18} className="text-amber-400" />
            <h2 className="text-lg font-display font-semibold text-amber-300">Community Edition</h2>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 mb-4 text-sm">
            <div>
              <span className="text-farm-500">Tier:</span>
              <span className="ml-2 font-medium text-amber-400 capitalize">{lic.tier}</span>
            </div>
            <div>
              <span className="text-farm-500">Printers:</span>
              <span className="ml-2 text-farm-200">{statsData?.printers?.total || 0} / {lic.max_printers === -1 ? '\u221E' : (lic.max_printers || 5)}</span>
            </div>
            <div>
              <span className="text-farm-500">Users:</span>
              <span className="ml-2 text-farm-200">{usersData?.length || 0} / {lic.max_users === -1 ? '\u221E' : (lic.max_users || 1)}</span>
            </div>
          </div>
          <a
            href="https://runsodin.com/pricing"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-2 px-4 py-2 bg-amber-500 hover:bg-amber-400 text-farm-950 font-semibold rounded-lg text-sm transition-colors"
          >
            Upgrade to Pro <ExternalLink size={14} />
          </a>
        </div>
      )}

      {/* Spoolman Integration */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4 flex-wrap">
          <Database size={18} className="text-print-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Spoolman Integration</h2>
          {statsData?.spoolman_connected ? (
            <span className="flex items-center gap-1 text-xs md:text-sm text-green-400 bg-green-900/30 px-2 py-0.5 rounded-lg">
              <CheckCircle size={14} /> Connected
            </span>
          ) : (
            <span className="flex items-center gap-1 text-xs md:text-sm text-farm-400 bg-farm-800 px-2 py-0.5 rounded-lg">
              <XCircle size={14} /> Not Connected
            </span>
          )}
        </div>
        <p className="text-sm text-farm-400 mb-4">
          Connect to Spoolman to track your filament inventory and automatically sync spool data.
        </p>
        <div className="flex flex-col sm:flex-row gap-3 md:gap-4">
          <div className="flex-1">
            <label className="block text-sm text-farm-400 mb-1">Spoolman URL</label>
            <input
              type="text"
              value={settings.spoolman_url}
              onChange={(e) => setSettings(s => ({ ...s, spoolman_url: e.target.value }))}
              placeholder="http://192.168.1.100:7912"
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
            />
          </div>
          <div className="flex items-end">
            <button
              onClick={() => testSpoolman.mutate()}
              disabled={!settings.spoolman_url}
              className="flex items-center gap-2 px-4 py-2 bg-farm-700 hover:bg-farm-600 disabled:opacity-50 rounded-lg text-sm w-full sm:w-auto justify-center"
            >
              <Plug size={16} /> Test Connection
            </button>
          </div>
        </div>
        {testSpoolman.isSuccess && (
          <p className={`mt-2 text-sm ${testSpoolman.data?.success ? 'text-green-400' : 'text-red-400'}`}>
            {testSpoolman.data?.message || (testSpoolman.data?.success ? 'Connection successful!' : 'Connection failed')}
          </p>
        )}
      </div>

      {/* Scheduler Settings */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <Clock size={18} className="text-print-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Scheduler Settings</h2>
        </div>
        <p className="text-sm text-farm-400 mb-4">
          Configure when the scheduler should avoid scheduling prints (e.g., overnight quiet hours).
        </p>
        <div className="grid grid-cols-2 gap-3 md:gap-4 max-w-md">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Blackout Start</label>
            <input type="time" value={settings.blackout_start} onChange={(e) => setSettings(s => ({ ...s, blackout_start: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Blackout End</label>
            <input type="time" value={settings.blackout_end} onChange={(e) => setSettings(s => ({ ...s, blackout_end: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
          </div>
        </div>
        <p className="text-xs text-farm-500 mt-2">
          Jobs will not be scheduled to start during blackout hours (currently {settings.blackout_start} - {settings.blackout_end})
        </p>
      </div>

      {/* Job Approval Workflow */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <CheckCircle size={18} className="text-print-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Job Approval Workflow</h2>
        </div>
        <p className="text-sm text-farm-400 mb-4">
          When enabled, viewer-role users (students) must have their print jobs approved by an operator or admin (teacher) before they can be scheduled. Operators and admins bypass approval.
        </p>
        <ApprovalToggle />
        <p className="text-xs text-farm-500 mt-2">Saves automatically when toggled.</p>
      </div>

      {/* Interface Mode */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <SettingsIcon size={18} className="text-print-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Interface Mode</h2>
        </div>
        <p className="text-sm text-farm-400 mb-4">
          Simple mode hides advanced features like Orders, Products, Analytics, and Maintenance for a cleaner sidebar.
        </p>
        <div className="flex gap-3">
          <button
            onClick={() => toggleUiMode('simple')}
            className={`flex-1 px-4 py-3 rounded-lg border text-sm font-medium transition-colors ${
              uiMode === 'simple'
                ? 'bg-print-600/20 border-print-500 text-print-300'
                : 'bg-farm-800 border-farm-700 text-farm-400 hover:border-farm-600'
            }`}
          >
            <div className="font-semibold mb-1">Simple</div>
            <div className="text-xs opacity-70">Essential features only</div>
            {uiMode === 'advanced' && <div className="text-xs text-amber-400/70 mt-1">Vigil AI settings tab will be hidden</div>}
          </button>
          <button
            onClick={() => toggleUiMode('advanced')}
            className={`flex-1 px-4 py-3 rounded-lg border text-sm font-medium transition-colors ${
              uiMode === 'advanced'
                ? 'bg-print-600/20 border-print-500 text-print-300'
                : 'bg-farm-800 border-farm-700 text-farm-400 hover:border-farm-600'
            }`}
          >
            <div className="font-semibold mb-1">Advanced</div>
            <div className="text-xs opacity-70">All features visible</div>
          </button>
        </div>
      </div>
      {/* Education Mode */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <SettingsIcon size={18} className="text-blue-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Education Mode</h2>
        </div>
        <p className="text-sm text-farm-400 mb-4">
          When enabled, commerce features (Orders, Products, Consumables) are hidden from the sidebar and UI. Ideal for educational environments that don't need e-commerce functionality.
        </p>
        <EducationModeToggle />
        <p className="text-xs text-farm-500 mt-2">Saves automatically when toggled. Users may need to refresh their browser.</p>
      </div>

      {/* Network — merged into General tab */}
      <NetworkTab />

      {/* Save Button - General */}
      <div className="flex items-center gap-4">
        <button onClick={handleSave} disabled={saveSettings.isPending} className="flex items-center gap-2 px-5 md:px-6 py-2 bg-print-600 hover:bg-print-500 disabled:opacity-50 rounded-lg font-medium text-sm">
          {saveSettings.isPending ? <RefreshCw size={16} className="animate-spin" /> : <Save size={16} />}
          Save Scheduling Settings
        </button>
        {saved && (
          <span className="flex items-center gap-1 text-green-400 text-sm">
            <CheckCircle size={14} /> Settings saved!
          </span>
        )}
      </div>

      </div>}

      {/* ==================== VISION AI TAB ==================== */}
      {activeTab === 'vision' && <VisionSettingsTab />}

      {/* ==================== SYSTEM TAB (Data + License) ==================== */}
      {activeTab === 'system' && <div className="max-w-4xl">
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

      {/* License — merged into System tab */}
      <div className="border-t border-farm-700 pt-6 mt-6">
        <LicenseTab />
      </div>

      </div>}
    </div>
  )
}
