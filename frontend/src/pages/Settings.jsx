import { useState, useEffect } from 'react'
import usePushNotifications from '../hooks/usePushNotifications'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Save, RefreshCw, Database, Clock, Plug, CheckCircle, XCircle, Download, Trash2, HardDrive, Plus, AlertTriangle, FileSpreadsheet, Bell, Mail, Smartphone, Settings as SettingsIcon, Users, Shield, Palette , Key, Webhook} from 'lucide-react'
import Admin from './Admin'
import Permissions from './Permissions'
import Branding from './Branding'
import OIDCSettings from '../components/OIDCSettings'
import WebhookSettings from '../components/WebhookSettings'

import { alertPreferences, smtpConfig } from '../api'
import { getApprovalSetting, setApprovalSetting } from '../api'
import { useLicense } from '../LicenseContext'

const API_KEY = import.meta.env.VITE_API_KEY
const apiHeaders = {
  'Content-Type': 'application/json',
  'X-API-Key': API_KEY
}

const backupsApi = {
  list: async () => {
    const res = await fetch('/api/backups', { headers: apiHeaders })
    if (!res.ok) throw new Error('Failed to list backups')
    return res.json()
  },
  create: async () => {
    const res = await fetch('/api/backups', { method: 'POST', headers: apiHeaders })
    if (!res.ok) throw new Error('Failed to create backup')
    return res.json()
  },
  remove: async (filename) => {
    const res = await fetch(`/api/backups/${filename}`, { method: 'DELETE', headers: apiHeaders })
    if (!res.ok) throw new Error('Failed to delete backup')
    return true
  },
  download: (filename) => {
    // Create a temporary link with the API key as a query param for download
    const a = document.createElement('a')
    a.href = `/api/backups/${filename}`
    a.download = filename
    // Use fetch + blob to include auth header
    return fetch(`/api/backups/${filename}`, { headers: { 'X-API-Key': API_KEY } })
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
      <div
        onClick={() => !isLoading && toggleMutation.mutate(!enabled)}
        className={`relative w-11 h-6 rounded-full transition-colors ${
          enabled ? "bg-print-600" : "bg-farm-700"
        }`}
      >
        <div className={`absolute top-0.5 w-5 h-5 rounded-full bg-white shadow transition-transform ${
          enabled ? "translate-x-[22px]" : "translate-x-0.5"
        }`} />
      </div>
      <span className="text-sm">
        {enabled ? "Approval required for viewer-role users" : "Approval disabled — all users create jobs directly"}
      </span>
    </label>
  )
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
    fetch('/api/pricing-config', { headers: { 'X-API-Key': API_KEY } })
      .then(r => r.json()).then(d => { if (d.ui_mode) setUiMode(d.ui_mode) }).catch(() => {})
  }, [])
  const toggleUiMode = async (mode) => {
    setUiMode(mode)
    try {
      const current = await fetch('/api/pricing-config', { headers: { 'X-API-Key': API_KEY } }).then(r => r.json())
      await fetch('/api/pricing-config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
        body: JSON.stringify({ ...current, ui_mode: mode })
      })
      window.dispatchEvent(new CustomEvent('ui-mode-changed', { detail: mode }))
    } catch (e) { console.error('Failed to save UI mode:', e) }
  }
  const saveSettings = useMutation({
    mutationFn: (data) => fetch('/api/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    onSuccess: () => { queryClient.invalidateQueries(['config']); queryClient.invalidateQueries(['stats']); setSaved(true); setTimeout(() => setSaved(false), 3000) },
  })

  const testSpoolman = useMutation({ mutationFn: () => fetch('/api/spoolman/test').then(r => r.json()) })

  const createBackup = useMutation({
    mutationFn: backupsApi.create,
    onSuccess: () => queryClient.invalidateQueries(['backups'])
  })

  const deleteBackup = useMutation({
    mutationFn: backupsApi.remove,
    onSuccess: () => {
      queryClient.invalidateQueries(['backups'])
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

  const PRO_TABS = ['sso', 'webhooks', 'email', 'users', 'permissions', 'branding']
  const ALL_TABS = [
    { id: 'general', label: 'General', icon: SettingsIcon },
    { id: 'alerts', label: 'Alerts', icon: Bell },
    { id: 'email', label: 'Email', icon: Mail },
    { id: 'sso', label: 'SSO', icon: Key },
    { id: 'webhooks', label: 'Webhooks', icon: Webhook },
    { id: 'users', label: 'Users', icon: Users },
    { id: 'permissions', label: 'Permissions', icon: Shield },
    { id: 'branding', label: 'Branding', icon: Palette },
    { id: 'data', label: 'Data', icon: Database },
  ]
  const TABS = lic.isPro ? ALL_TABS : ALL_TABS.filter(t => !PRO_TABS.includes(t.id))

  return (
    <div className="p-4 md:p-8">
      <div className="mb-4 md:mb-6">
        <h1 className="text-2xl md:text-3xl font-display font-bold">Settings</h1>
        <p className="text-farm-500 text-sm mt-1">Configure your print farm</p>
      </div>

      {/* Tab Bar */}
      <div className="flex gap-1 mb-6 overflow-x-auto pb-1 -mx-1 px-1">
        {TABS.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors ${
              activeTab === tab.id
                ? 'bg-print-600 text-white'
                : 'bg-farm-900 text-farm-400 hover:bg-farm-800 hover:text-farm-200'
            }`}
          >
            <tab.icon size={16} />
            <span className="hidden sm:inline">{tab.label}</span>
          </button>
        ))}
      </div>

      {/* ==================== USERS TAB ==================== */}
      {activeTab === 'sso' && <div className="max-w-4xl">
        <OIDCSettings />
      </div>}

      {activeTab === 'webhooks' && <div className="max-w-4xl">
        <WebhookSettings />
      </div>}

      {activeTab === 'users' && <Admin />}

      {/* ==================== PERMISSIONS TAB ==================== */}
      {activeTab === 'permissions' && <Permissions />}

      {/* ==================== BRANDING TAB ==================== */}
      {activeTab === 'branding' && <Branding />}

      {/* ==================== GENERAL TAB ==================== */}
      {activeTab === 'alerts' && <div className="max-w-4xl">
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
      <div className="bg-farm-900 rounded border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
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
                          onClick={() => toggleAlertPref(pref.alert_type, 'in_app')}
                          className={`w-10 h-5 rounded-full transition-colors relative ${pref.in_app ? 'bg-print-600' : 'bg-farm-600'}`}
                        >
                          <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${pref.in_app ? 'left-5' : 'left-0.5'}`} />
                        </button>
                      </div>

                      <div className="flex md:justify-center items-center gap-4 md:gap-0 mt-1 md:mt-0 ml-6 md:ml-0">
                        <span className="text-xs text-farm-500 md:hidden mr-1">Push</span>
                        <button
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

      </div>}

      {/* ==================== EMAIL TAB ==================== */}
      {activeTab === 'email' && <div className="max-w-4xl">
      {/* SMTP Email Configuration */}
      <div className="bg-farm-900 rounded border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <Mail size={18} className="text-print-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Email Notifications (SMTP)</h2>
          <button
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
                  placeholder="printfarm@yourdomain.com"
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

      </div>}

      {/* ==================== GENERAL TAB (continued) ==================== */}
      {activeTab === 'general' && <div className="max-w-4xl">
      {/* Spoolman Integration */}
      <div className="bg-farm-900 rounded border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4 flex-wrap">
          <Database size={18} className="text-print-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Spoolman Integration</h2>
          {statsData?.spoolman_connected ? (
            <span className="flex items-center gap-1 text-xs md:text-sm text-green-400 bg-green-900/30 px-2 py-0.5 rounded">
              <CheckCircle size={14} /> Connected
            </span>
          ) : (
            <span className="flex items-center gap-1 text-xs md:text-sm text-farm-400 bg-farm-800 px-2 py-0.5 rounded">
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
      <div className="bg-farm-900 rounded border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
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
      <div className="bg-farm-900 rounded border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <CheckCircle size={18} className="text-print-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Job Approval Workflow</h2>
        </div>
        <p className="text-sm text-farm-400 mb-4">
          When enabled, viewer-role users (students) must have their print jobs approved by an operator or admin (teacher) before they can be scheduled. Operators and admins bypass approval.
        </p>
        <ApprovalToggle />
      </div>

      {/* Interface Mode */}
      <div className="bg-farm-900 rounded border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
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
      {/* Save Button - General */}
      <div className="flex items-center gap-4">
        <button onClick={handleSave} disabled={saveSettings.isPending} className="flex items-center gap-2 px-5 md:px-6 py-2 bg-print-600 hover:bg-print-500 disabled:opacity-50 rounded-lg font-medium text-sm">
          {saveSettings.isPending ? <RefreshCw size={16} className="animate-spin" /> : <Save size={16} />}
          Save Settings
        </button>
        {saved && (
          <span className="flex items-center gap-1 text-green-400 text-sm">
            <CheckCircle size={14} /> Settings saved!
          </span>
        )}
      </div>

      </div>}

      {/* ==================== DATA TAB ==================== */}
      {activeTab === 'data' && <div className="max-w-4xl">
      {/* Database Info */}
      <div className="bg-farm-900 rounded border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
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
      <div className="bg-farm-900 rounded border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
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
                    className="p-2 bg-farm-700 hover:bg-farm-600 rounded text-farm-200 hover:text-white transition-colors"
                    title="Download backup"
                  >
                    <Download size={16} />
                  </button>
                  {deleteConfirm === backup.filename ? (
                    <div className="flex items-center gap-1">
                      <button
                        onClick={() => deleteBackup.mutate(backup.filename)}
                        disabled={deleteBackup.isPending}
                        className="px-2 py-1.5 bg-red-600 hover:bg-red-500 rounded text-white text-xs font-medium"
                      >
                        Confirm
                      </button>
                      <button
                        onClick={() => setDeleteConfirm(null)}
                        className="px-2 py-1.5 bg-farm-700 hover:bg-farm-600 rounded text-farm-200 text-xs"
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={() => setDeleteConfirm(backup.filename)}
                      className="p-2 bg-farm-700 hover:bg-red-900 rounded text-farm-200 hover:text-red-400 transition-colors"
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
      </div>

      {/* Data Export */}
      <div className="bg-farm-900 rounded border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <FileSpreadsheet size={18} className="text-print-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Data Export</h2>
        </div>
        <p className="text-sm text-farm-400 mb-4">
          Export your data as CSV files for analysis, reporting, or backup purposes.
        </p>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <a
            href="/api/export/jobs"
            download="jobs_export.csv"
            className="flex items-center justify-center gap-2 px-4 py-3 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm font-medium transition-colors"
          >
            <Download size={16} />
            Jobs
          </a>
          <a
            href="/api/export/models"
            download="models_export.csv"
            className="flex items-center justify-center gap-2 px-4 py-3 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm font-medium transition-colors"
          >
            <Download size={16} />
            Models
          </a>
          <a
            href="/api/export/spools"
            download="spools_export.csv"
            className="flex items-center justify-center gap-2 px-4 py-3 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm font-medium transition-colors"
          >
            <Download size={16} />
            Spools
          </a>
          <a
            href="/api/export/filament-usage"
            download="filament_usage_export.csv"
            className="flex items-center justify-center gap-2 px-4 py-3 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm font-medium transition-colors"
          >
            <Download size={16} />
            Filament Usage
          </a>
        </div>
      </div>

      </div>}
    </div>
  )
}
