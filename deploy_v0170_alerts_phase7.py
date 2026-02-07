"""
Phase 7: Add Alert Preferences section to Settings page.

Adds:
- Alert Preferences card with toggle matrix
- Per-alert-type toggles: in-app, browser push, email
- Spool low threshold slider (50-500g)
- Uses alertPreferences API from api.js (already added in Phase 2)
"""
import os

SETTINGS_PATH = "/opt/printfarm-scheduler/frontend/src/pages/Settings.jsx"


def patch_settings():
    with open(SETTINGS_PATH, "r") as f:
        content = f.read()

    # 1. Add Bell import to lucide-react imports
    content = content.replace(
        "import { Save, RefreshCw, Database, Clock, Plug, CheckCircle, XCircle, Download, Trash2, HardDrive, Plus, AlertTriangle, FileSpreadsheet } from 'lucide-react'",
        "import { Save, RefreshCw, Database, Clock, Plug, CheckCircle, XCircle, Download, Trash2, HardDrive, Plus, AlertTriangle, FileSpreadsheet, Bell, Mail, Smartphone } from 'lucide-react'"
    )

    # 2. Add alertPreferences import from api.js
    content = content.replace(
        "const API_KEY = import.meta.env.VITE_API_KEY",
        """import { alertPreferences } from '../api'

const API_KEY = import.meta.env.VITE_API_KEY"""
    )

    # 3. Add alert preferences state and logic inside the component, after deleteConfirm state
    content = content.replace(
        "  const [deleteConfirm, setDeleteConfirm] = useState(null)",
        """  const [deleteConfirm, setDeleteConfirm] = useState(null)
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

  const normalizeType = (type) => type.toLowerCase()"""
    )

    # 4. Add the Alert Preferences section before Spoolman Integration
    content = content.replace(
        "      {/* Spoolman Integration */}",
        """      {/* Alert Preferences */}
      <div className="bg-farm-900 rounded-xl border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
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

      {/* Spoolman Integration */}"""
    )

    with open(SETTINGS_PATH, "w") as f:
        f.write(content)
    print("  Settings.jsx: Added Alert Preferences section")


def verify():
    with open(SETTINGS_PATH, "r") as f:
        content = f.read()
    checks = [
        ("alertPreferences import", "import { alertPreferences }" in content),
        ("Bell icon import", "Bell, Mail, Smartphone" in content),
        ("Alert Preferences section", "Alert Preferences" in content),
        ("toggleAlertPref function", "toggleAlertPref" in content),
        ("Save Preferences button", "Save Preferences" in content),
        ("spool_low threshold slider", 'typeKey === \'spool_low\'' in content),
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
    print("Phase 7: Alert Preferences UI")
    print("=" * 50)
    print()
    print("[1/2] Patching Settings.jsx...")
    patch_settings()
    print()
    print("[2/2] Verifying...")
    if verify():
        print()
        print("All checks passed! Vite should auto-reload.")
        print("Go to Settings page to see Alert Preferences section.")
    else:
        print()
        print("Some checks failed — review manually.")


if __name__ == "__main__":
    main()
