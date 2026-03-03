import { useState, useEffect } from 'react'
import { Bell, Mail, Save, CheckCircle, AlertTriangle, RefreshCw, XCircle } from 'lucide-react'
import usePushNotifications from '../../hooks/usePushNotifications'
import { alertPreferences, smtpConfig } from '../../api'
import { useLicense } from '../../LicenseContext'
import { Button, Input, Card } from '../ui'

export default function NotificationsTab() {
  const lic = useLicense()
  const { isSupported: pushSupported, isSubscribed: pushSubscribed, permission: pushPermission,
          loading: pushLoading, subscribe: subscribePush, unsubscribe: unsubscribePush } = usePushNotifications()

  const [alertPrefs, setAlertPrefs] = useState([])
  const [alertPrefsLoading, setAlertPrefsLoading] = useState(true)
  const [alertPrefsSaved, setAlertPrefsSaved] = useState(false)
  const [alertPrefsError, setAlertPrefsError] = useState(null)

  // Load alert preferences
  useEffect(() => {
    const loadAlertPrefs = async () => {
      try {
        const data = await alertPreferences.get()
        // Ensure we have all 4 types
        const types = ['print_complete', 'print_failed', 'spool_low', 'maintenance_overdue', 'bed_cooled', 'queue_added', 'queue_skipped', 'queue_failed_start']
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
          { alert_type: 'bed_cooled', in_app: true, browser_push: false, email: false, threshold_value: 40 },
          { alert_type: 'queue_added', in_app: false, browser_push: false, email: false, threshold_value: null },
          { alert_type: 'queue_skipped', in_app: true, browser_push: false, email: false, threshold_value: null },
          { alert_type: 'queue_failed_start', in_app: true, browser_push: true, email: false, threshold_value: null },
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
    'bed_cooled': { label: 'Bed Cooled', desc: 'When bed temp drops below threshold after print', icon: '🧊' },
    'queue_added': { label: 'Job Queued', desc: 'When a job enters the print queue', icon: '📥' },
    'queue_skipped': { label: 'Job Skipped', desc: 'When a job is skipped (filament/printer mismatch)', icon: '⏭' },
    'queue_failed_start': { label: 'Job Failed to Start', desc: 'When a job fails to dispatch to printer', icon: '⚠' },
  }

  const normalizeType = (type) => type.toLowerCase()

  // SMTP config state
  const [smtp, setSmtp] = useState({ enabled: false, host: '', port: 587, username: '', password: '', from_address: '' })
  const [smtpLoading, setSmtpLoading] = useState(true)
  const [smtpSaved, setSmtpSaved] = useState(false)
  const [smtpError, setSmtpError] = useState(null)
  const [smtpTesting, setSmtpTesting] = useState(false)
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

  return (
    <div className="max-w-4xl">
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
            <Button
              variant={pushSubscribed ? 'tertiary' : 'primary'}
              loading={pushLoading}
              disabled={pushPermission === 'denied'}
              onClick={() => pushSubscribed ? unsubscribePush() : subscribePush()}
            >
              {pushSubscribed ? 'Disable Push' : 'Enable Push'}
            </Button>
          </div>
        </div>
      )}


      {/* Alert Preferences */}
      <Card padding="lg" className="mb-4 md:mb-6">
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
              <Button variant="primary" icon={Save} onClick={saveAlertPrefs}>
                Save Preferences
              </Button>
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
      </Card>

      {/* SMTP Email Configuration — merged into Notifications tab */}
      {lic.isPro && <>
      <Card padding="lg" className="mb-4 md:mb-6">
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
                <Input
                  label="SMTP Host"
                  type="text"
                  value={smtp.host}
                  onChange={(e) => setSmtp(s => ({ ...s, host: e.target.value }))}
                  placeholder="smtp.gmail.com"
                />
                <Input
                  label="Port"
                  type="number"
                  value={smtp.port}
                  onChange={(e) => setSmtp(s => ({ ...s, port: parseInt(e.target.value) || 587 }))}
                />
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <Input
                  label="Username"
                  type="text"
                  value={smtp.username}
                  onChange={(e) => setSmtp(s => ({ ...s, username: e.target.value }))}
                  placeholder="your@email.com"
                />
                <div>
                  <label className="block text-sm text-farm-400 mb-1">Password</label>
                  <div className="relative">
                    <Input
                      type={showSmtpPassword ? 'text' : 'password'}
                      value={smtp.password}
                      onChange={(e) => setSmtp(s => ({ ...s, password: e.target.value }))}
                      placeholder={smtp._password_set ? '--------  (saved)' : 'App password or SMTP password'}
                      className="pr-16"
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

              <Input
                label="From Address"
                type="text"
                value={smtp.from_address}
                onChange={(e) => setSmtp(s => ({ ...s, from_address: e.target.value }))}
                placeholder="odin@yourdomain.com"
                wrapperClassName="max-w-md"
              />
            </div>

            <div className="flex items-center gap-3 mt-4 flex-wrap">
              <Button variant="primary" icon={Save} onClick={saveSmtp}>
                Save SMTP
              </Button>
              <Button
                variant="tertiary"
                icon={Mail}
                loading={smtpTesting}
                disabled={!smtp.enabled || !smtp.host}
                onClick={testEmail}
              >
                Send Test Email
              </Button>
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
      </Card>

      </>}
    </div>
  )
}
