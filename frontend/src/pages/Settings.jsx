import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Save, RefreshCw, Database, Clock, Plug, CheckCircle, XCircle, Download, Trash2, HardDrive, Plus, AlertTriangle, FileSpreadsheet } from 'lucide-react'

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

export default function Settings() {
  const queryClient = useQueryClient()
  const [settings, setSettings] = useState({
    spoolman_url: '',
    blackout_start: '22:30',
    blackout_end: '05:30',
  })
  const [saved, setSaved] = useState(false)
  const [deleteConfirm, setDeleteConfirm] = useState(null)

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

      {/* Database Backups */}
      <div className="bg-farm-900 rounded-xl border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
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
      <div className="bg-farm-900 rounded-xl border border-farm-800 p-4 md:p-6 mb-4 md:mb-6">
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
