"""
Phase 6: Add SMTP Configuration section to Settings page.

Adds:
- SMTP config card with host, port, username, password, from address
- Enable/disable toggle
- Test Email button
- Positioned after Alert Preferences, before Spoolman
"""
import os

SETTINGS_PATH = "/opt/printfarm-scheduler/frontend/src/pages/Settings.jsx"


def patch_settings():
    with open(SETTINGS_PATH, "r") as f:
        content = f.read()

    # 1. Add smtpConfig import alongside alertPreferences
    content = content.replace(
        "import { alertPreferences } from '../api'",
        "import { alertPreferences, smtpConfig } from '../api'"
    )

    # 2. Add SMTP state after alertPrefsError state
    content = content.replace(
        """  const normalizeType = (type) => type.toLowerCase()""",
        """  const normalizeType = (type) => type.toLowerCase()

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
  }"""
    )

    # 3. Add SMTP config section after Alert Preferences, before Spoolman
    content = content.replace(
        "      {/* Spoolman Integration */}",
        """      {/* SMTP Email Configuration */}
      <div className="bg-farm-900 rounded-xl border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
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

      {/* Spoolman Integration */}"""
    )

    with open(SETTINGS_PATH, "w") as f:
        f.write(content)
    print("  Settings.jsx: Added SMTP Configuration section")


def verify():
    with open(SETTINGS_PATH, "r") as f:
        content = f.read()
    checks = [
        ("smtpConfig import", "smtpConfig" in content),
        ("SMTP state vars", "smtpLoading" in content),
        ("SMTP config section", "Email Notifications (SMTP)" in content),
        ("Host input", 'smtp.gmail.com' in content),
        ("Test Email button", "Send Test Email" in content),
        ("Password show/hide", "showSmtpPassword" in content),
        ("Gmail tip", "App Password" in content),
    ]
    all_pass = True
    for name, result in checks:
        status = "✅" if result else "❌"
        print(f"  {status} {name}")
        if not result:
            all_pass = False
    return all_pass


def main():
    print("=" * 50)
    print("Phase 6: SMTP Email Configuration UI")
    print("=" * 50)
    print()
    print("[1/2] Patching Settings.jsx...")
    patch_settings()
    print()
    print("[2/2] Verifying...")
    if verify():
        print()
        print("All checks passed! Vite should auto-reload.")
        print("Go to Settings to see SMTP section below Alert Preferences.")
    else:
        print()
        print("Some checks failed — review manually.")


if __name__ == "__main__":
    main()
