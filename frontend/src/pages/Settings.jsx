import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Save, RefreshCw, Database, Clock, Plug, CheckCircle, XCircle } from 'lucide-react'

export default function Settings() {
  const queryClient = useQueryClient()
  const [settings, setSettings] = useState({
    spoolman_url: '',
    blackout_start: '22:30',
    blackout_end: '05:30',
  })
  const [saved, setSaved] = useState(false)

  const { data: statsData } = useQuery({ queryKey: ['stats'], queryFn: () => fetch('/api/stats').then(r => r.json()) })
  const { data: configData } = useQuery({ queryKey: ['config'], queryFn: () => fetch('/api/config').then(r => r.json()) })

  useEffect(() => {
    if (configData) {
      setSettings({
        spoolman_url: configData.spoolman_url || '',
        blackout_start: configData.blackout_start || '22:30',
        blackout_end: configData.blackout_end || '05:30',
      })
    }
  }, [configData])

  const saveSettings = useMutation({
    mutationFn: (data) => fetch('/api/config', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }).then(r => r.json()),
    onSuccess: () => { queryClient.invalidateQueries(['config']); queryClient.invalidateQueries(['stats']); setSaved(true); setTimeout(() => setSaved(false), 3000) },
  })

  const testSpoolman = useMutation({ mutationFn: () => fetch('/api/spoolman/test').then(r => r.json()) })

  const handleSave = () => saveSettings.mutate(settings)

  return (
    <div className="p-4 md:p-8 max-w-4xl">
      <div className="mb-4 md:mb-6">
        <h1 className="text-2xl md:text-3xl font-display font-bold">Settings</h1>
        <p className="text-farm-500 text-sm mt-1">Configure your print farm</p>
      </div>

      {/* Spoolman Integration */}
      <div className="bg-farm-900 rounded-xl border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
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
      <div className="bg-farm-900 rounded-xl border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
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

      {/* Database Info */}
      <div className="bg-farm-900 rounded-xl border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
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

      {/* Save Button */}
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
    </div>
  )
}
