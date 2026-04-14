import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Save, RefreshCw, Database, Clock, Plug, CheckCircle, XCircle, Settings as SettingsIcon, Zap, Wifi, ExternalLink } from 'lucide-react'
import toast from 'react-hot-toast'
import { config as configApi, pricingConfig, fetchAPI, users as usersApi } from '../../api'
import { useLicense } from '../../LicenseContext'
import { getApprovalSetting, setApprovalSetting } from '../../api'
import { getEducationMode, setEducationMode } from '../../api'
import { Button, Input } from '../ui'

function ApprovalToggle() {
  const queryClient = useQueryClient()
  const { data: setting, isLoading } = useQuery({
    queryKey: ["approval-setting"],
    queryFn: getApprovalSetting,
  })

  const toggleMutation = useMutation({
    mutationFn: (enabled) => setApprovalSetting(enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["approval-setting"] }),
    onError: (err: any) => toast.error('Toggle approval failed: ' + (err?.message || 'Unknown error')),
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
          enabled ? "bg-[var(--brand-primary)]" : "bg-[var(--brand-input-bg)]"
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
    },
    onError: (err: any) => toast.error('Toggle education mode failed: ' + (err?.message || 'Unknown error')),
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
          enabled ? "bg-blue-600" : "bg-[var(--brand-input-bg)]"
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
    <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 md:p-6 mb-4 md:mb-6">
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
      queryClient.invalidateQueries({ queryKey: ['pricing-config'] })
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
    // v1.8.8 audit: don't let server-side validation errors vanish
    // into "nothing happened" from the operator's POV.
    onError: (err: any) => toast.error('Save failed: ' + (err?.message || 'Unknown error')),
  })

  const testSpoolman = useMutation({
    mutationFn: () => configApi.testSpoolman(),
    onError: (err: any) => toast.error('Spoolman test failed: ' + (err?.message || 'Unknown error')),
  })

  const handleSave = () => saveSettings.mutate(settings)

  return (
    <div className="max-w-4xl">

      {/* Upgrade Card (Community only) */}
      {!lic.isPro && (
        <div className="bg-amber-900/20 rounded-md border border-amber-700/30 p-4 md:p-6 mb-4 md:mb-6">
          <div className="flex items-center gap-2 mb-3">
            <Zap size={18} className="text-[var(--brand-primary)]" />
            <h2 className="text-lg font-display font-semibold text-amber-300">Community Edition</h2>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-4 mb-4 text-sm">
            <div>
              <span className="text-[var(--brand-text-muted)]">Tier:</span>
              <span className="ml-2 font-medium text-[var(--brand-primary)] capitalize">{lic.tier}</span>
            </div>
            <div>
              <span className="text-[var(--brand-text-muted)]">Printers:</span>
              <span className="ml-2 text-[var(--brand-text-primary)]">{statsData?.printers?.total || 0} / {lic.max_printers === -1 ? '\u221E' : (lic.max_printers || 5)}</span>
            </div>
            <div>
              <span className="text-[var(--brand-text-muted)]">Users:</span>
              <span className="ml-2 text-[var(--brand-text-primary)]">{usersData?.length || 0} / {lic.max_users === -1 ? '\u221E' : (lic.max_users || 1)}</span>
            </div>
          </div>
          <div className="flex flex-wrap gap-3">
            <a
              href="https://runsodin.com/pricing"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 px-4 py-2 bg-amber-500 hover:bg-amber-400 text-[var(--brand-content-bg)] font-semibold rounded-md text-sm transition-colors"
            >
              Upgrade to Pro <ExternalLink size={14} />
            </a>
            <a
              href="https://docs.runsodin.com"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 px-4 py-2 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-input-bg)] text-[var(--brand-text-primary)] font-semibold rounded-md text-sm transition-colors border border-[var(--brand-card-border)]"
            >
              Documentation <ExternalLink size={14} />
            </a>
          </div>
        </div>
      )}

      {/* Spoolman Integration */}
      <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4 flex-wrap">
          <Database size={18} className="text-[var(--brand-primary)]" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Spoolman Integration</h2>
          {statsData?.spoolman_connected ? (
            <span className="flex items-center gap-1 text-xs md:text-sm text-green-400 bg-green-900/30 px-2 py-0.5 rounded-md">
              <CheckCircle size={14} /> Connected
            </span>
          ) : (
            <span className="flex items-center gap-1 text-xs md:text-sm text-[var(--brand-text-secondary)] bg-[var(--brand-input-bg)] px-2 py-0.5 rounded-md">
              <XCircle size={14} /> Not Connected
            </span>
          )}
        </div>
        <p className="text-sm text-[var(--brand-text-secondary)] mb-4">
          Connect to Spoolman to track your filament inventory and automatically sync spool data.
        </p>
        <div className="flex flex-col sm:flex-row gap-3 md:gap-4">
          <div className="flex-1">
            <Input
              label="Spoolman URL"
              type="text"
              value={settings.spoolman_url}
              onChange={(e) => setSettings(s => ({ ...s, spoolman_url: e.target.value }))}
              placeholder="http://192.168.1.100:7912"
            />
          </div>
          <div className="flex items-end">
            <Button
              variant="tertiary"
              icon={Plug}
              onClick={() => testSpoolman.mutate()}
              disabled={!settings.spoolman_url}
              className="w-full sm:w-auto"
            >
              Test Connection
            </Button>
          </div>
        </div>
        {testSpoolman.isSuccess && (
          <p className={`mt-2 text-sm ${testSpoolman.data?.success ? 'text-green-400' : 'text-red-400'}`}>
            {testSpoolman.data?.message || (testSpoolman.data?.success ? 'Connection successful!' : 'Connection failed')}
          </p>
        )}
      </div>

      {/* Scheduler Settings */}
      <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <Clock size={18} className="text-[var(--brand-primary)]" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Scheduler Settings</h2>
        </div>
        <p className="text-sm text-[var(--brand-text-secondary)] mb-4">
          Configure when the scheduler should avoid scheduling prints (e.g., overnight quiet hours).
        </p>
        <div className="grid grid-cols-2 gap-3 md:gap-4 max-w-md">
          <Input
            label="Blackout Start"
            type="time"
            value={settings.blackout_start}
            onChange={(e) => setSettings(s => ({ ...s, blackout_start: e.target.value }))}
          />
          <Input
            label="Blackout End"
            type="time"
            value={settings.blackout_end}
            onChange={(e) => setSettings(s => ({ ...s, blackout_end: e.target.value }))}
          />
        </div>
        <p className="text-xs text-[var(--brand-text-muted)] mt-2">
          Jobs will not be scheduled to start during blackout hours (currently {settings.blackout_start} - {settings.blackout_end})
        </p>
      </div>

      {/* Job Approval Workflow */}
      <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <CheckCircle size={18} className="text-[var(--brand-primary)]" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Job Approval Workflow</h2>
        </div>
        <p className="text-sm text-[var(--brand-text-secondary)] mb-4">
          When enabled, viewer-role users (students) must have their print jobs approved by an operator or admin (teacher) before they can be scheduled. Operators and admins bypass approval.
        </p>
        <ApprovalToggle />
        <p className="text-xs text-[var(--brand-text-muted)] mt-2">Saves automatically when toggled.</p>
      </div>

      {/* Interface Mode */}
      <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <SettingsIcon size={18} className="text-[var(--brand-primary)]" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Interface Mode</h2>
        </div>
        <p className="text-sm text-[var(--brand-text-secondary)] mb-4">
          Simple mode hides advanced features like Orders, Products, Analytics, and Maintenance for a cleaner sidebar.
        </p>
        <div className="flex gap-3">
          <button
            onClick={() => toggleUiMode('simple')}
            className={`flex-1 px-4 py-3 rounded-md border text-sm font-medium transition-colors ${
              uiMode === 'simple'
                ? 'bg-[var(--brand-primary)]/20 border-[var(--brand-primary)] text-[var(--brand-primary)]'
                : 'bg-[var(--brand-input-bg)] border-[var(--brand-card-border)] text-[var(--brand-text-secondary)] hover:border-[var(--brand-card-border)]'
            }`}
          >
            <div className="font-semibold mb-1">Simple</div>
            <div className="text-xs opacity-70">Essential features only</div>
            {uiMode === 'advanced' && <div className="text-xs text-amber-400/70 mt-1">Vigil AI settings tab will be hidden</div>}
          </button>
          <button
            onClick={() => toggleUiMode('advanced')}
            className={`flex-1 px-4 py-3 rounded-md border text-sm font-medium transition-colors ${
              uiMode === 'advanced'
                ? 'bg-[var(--brand-primary)]/20 border-[var(--brand-primary)] text-[var(--brand-primary)]'
                : 'bg-[var(--brand-input-bg)] border-[var(--brand-card-border)] text-[var(--brand-text-secondary)] hover:border-[var(--brand-card-border)]'
            }`}
          >
            <div className="font-semibold mb-1">Advanced</div>
            <div className="text-xs opacity-70">All features visible</div>
          </button>
        </div>
      </div>
      {/* Education Mode */}
      <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 md:p-6 mb-4 md:mb-6">
        <div className="flex items-center gap-2 md:gap-3 mb-4">
          <SettingsIcon size={18} className="text-blue-400" />
          <h2 className="text-lg md:text-xl font-display font-semibold">Education Mode</h2>
        </div>
        <p className="text-sm text-[var(--brand-text-secondary)] mb-4">
          When enabled, commerce features (Orders, Products, Consumables) are hidden from the sidebar and UI. Ideal for educational environments that don't need e-commerce functionality.
        </p>
        <EducationModeToggle />
        <p className="text-xs text-[var(--brand-text-muted)] mt-2">Saves automatically when toggled. Users may need to refresh their browser.</p>
      </div>

      {/* Network — merged into General tab */}
      <NetworkSection />

      {/* Save Button - General */}
      <div className="flex items-center gap-4">
        <Button variant="primary" icon={Save} loading={saveSettings.isPending} onClick={handleSave}>
          Save Scheduling Settings
        </Button>
        {saved && (
          <span className="flex items-center gap-1 text-green-400 text-sm">
            <CheckCircle size={14} /> Settings saved!
          </span>
        )}
      </div>

    </div>
  )
}
