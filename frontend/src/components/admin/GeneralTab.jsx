import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Save, RefreshCw, Database, Clock, Plug, CheckCircle, XCircle, Settings as SettingsIcon, Zap, Wifi, ExternalLink } from 'lucide-react'
import { config as configApi, pricingConfig, fetchAPI, users as usersApi } from '../../api'
import { useLicense } from '../../LicenseContext'
import { getApprovalSetting, setApprovalSetting } from '../../api'
import { getEducationMode, setEducationMode } from '../../api'

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

function NetworkSection() {
  const [hostIp, setHostIp] = useState('')
  const [detectedIp, setDetectedIp] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    configApi.getNetwork()
      .then(data => {
        setDetectedIp(data.detected_ip || '')
        const configuredRow = data.configured_ip
        if (configuredRow) setHostIp(configuredRow)
        else if (data.detected_ip) setHostIp(data.detected_ip)
      })
      .catch(() => {})
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

export default function GeneralTab() {
  const queryClient = useQueryClient()
  const lic = useLicense()
  const [settings, setSettings] = useState({
    spoolman_url: '',
    blackout_start: '22:30',
    blackout_end: '05:30',
  })
  const [saved, setSaved] = useState(false)
  const [uiMode, setUiMode] = useState('advanced')

  const { data: statsData } = useQuery({ queryKey: ['stats'], queryFn: () => fetchAPI('/stats') })
  const { data: configData } = useQuery({ queryKey: ['config'], queryFn: configApi.get })
  const { data: usersData } = useQuery({ queryKey: ['users-count'], queryFn: usersApi.list, enabled: !lic.isPro })

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
    pricingConfig.get()
      .then(d => { if (d.ui_mode) setUiMode(d.ui_mode) }).catch(() => {})
  }, [])

  const toggleUiMode = async (mode) => {
    setUiMode(mode)
    try {
      const current = await pricingConfig.get()
      await pricingConfig.update({ ...current, ui_mode: mode })
      window.dispatchEvent(new CustomEvent('ui-mode-changed', { detail: mode }))
    } catch (e) { console.error('Failed to save UI mode:', e) }
  }

  const saveSettings = useMutation({
    mutationFn: (data) => configApi.update(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['config'] })
      queryClient.invalidateQueries({ queryKey: ['stats'] })
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    },
  })

  const testSpoolman = useMutation({ mutationFn: () => configApi.testSpoolman() })

  const handleSave = () => saveSettings.mutate(settings)

  return (
    <div className="max-w-4xl">

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
          <div className="flex flex-wrap gap-3">
            <a
              href="https://runsodin.com/pricing"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 px-4 py-2 bg-amber-500 hover:bg-amber-400 text-farm-950 font-semibold rounded-lg text-sm transition-colors"
            >
              Upgrade to Pro <ExternalLink size={14} />
            </a>
            <a
              href="https://docs.runsodin.com"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 px-4 py-2 bg-farm-800 hover:bg-farm-700 text-farm-200 font-semibold rounded-lg text-sm transition-colors border border-farm-700"
            >
              Documentation <ExternalLink size={14} />
            </a>
          </div>
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
      <NetworkSection />

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

    </div>
  )
}
