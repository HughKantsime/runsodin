import { useState, useEffect } from 'react'
import { Clock, Save, Loader2, Trash2, AlertTriangle } from 'lucide-react'
import { retention } from '../api'

const FIELDS = [
  { key: 'completed_jobs_days', label: 'Completed jobs', desc: '0 = keep forever' },
  { key: 'audit_logs_days', label: 'Audit logs', desc: 'Default: 365 days' },
  { key: 'timelapses_days', label: 'Timelapses', desc: 'Default: 30 days' },
  { key: 'alert_history_days', label: 'Alert history', desc: 'Default: 90 days' },
]

export default function DataRetentionSettings() {
  const [config, setConfig] = useState({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [cleaning, setCleaning] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [cleanResult, setCleanResult] = useState(null)

  useEffect(() => {
    retention.get().then(data => {
      setConfig(data)
      setLoading(false)
    }).catch(() => setLoading(false))
  }, [])

  const handleSave = async () => {
    setSaving(true)
    setError('')
    setSuccess('')
    try {
      const data = await retention.set(config)
      setConfig(data)
      setSuccess('Retention policy saved')
      setTimeout(() => setSuccess(''), 3000)
    } catch (err) {
      setError(err.message)
    }
    setSaving(false)
  }

  const handleCleanup = async () => {
    if (!confirm('Run data cleanup now? This will permanently delete data older than the configured retention periods.')) return
    setCleaning(true)
    setError('')
    try {
      const result = await retention.runCleanup()
      setCleanResult(result.deleted)
    } catch (err) {
      setError(err.message)
    }
    setCleaning(false)
  }

  if (loading) return null

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <Clock size={20} className="text-print-400" />
        <h3 className="text-lg font-semibold">Data Retention</h3>
      </div>
      <p className="text-sm text-farm-400 mb-4">Configure how long data is kept before automatic cleanup. Set to 0 to keep forever.</p>

      {error && (
        <div className="mb-3 p-3 bg-red-900/30 border border-red-700 rounded-lg flex items-center gap-2 text-sm text-red-400">
          <AlertTriangle size={16} /> {error}
        </div>
      )}
      {success && (
        <div className="mb-3 p-3 bg-green-900/30 border border-green-700 rounded-lg text-sm text-green-400">{success}</div>
      )}

      <div className="space-y-3 mb-4">
        {FIELDS.map(f => (
          <div key={f.key} className="flex items-center gap-3">
            <label className="w-40 text-sm text-farm-300">{f.label}</label>
            <input type="number" min="0" className="w-24 bg-farm-800 border border-farm-700 rounded px-3 py-1.5 text-sm"
              value={config[f.key] ?? ''} placeholder="0"
              onChange={e => setConfig(prev => ({ ...prev, [f.key]: parseInt(e.target.value) || 0 }))} />
            <span className="text-xs text-farm-500">days</span>
            <span className="text-[10px] text-farm-600">{f.desc}</span>
          </div>
        ))}
      </div>

      <div className="flex gap-3">
        <button onClick={handleSave} disabled={saving}
          className="flex items-center gap-2 px-4 py-2 rounded bg-print-600 hover:bg-print-700 text-white text-sm disabled:opacity-50">
          {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
          Save Policy
        </button>
        <button onClick={handleCleanup} disabled={cleaning}
          className="flex items-center gap-2 px-4 py-2 rounded bg-orange-600/20 hover:bg-orange-600/30 text-orange-400 text-sm border border-orange-700 disabled:opacity-50">
          {cleaning ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
          Run Cleanup Now
        </button>
      </div>

      {cleanResult && (
        <div className="mt-3 p-3 bg-farm-900 rounded-lg border border-farm-700 text-sm">
          <p className="text-farm-300 font-medium mb-1">Cleanup complete:</p>
          {Object.entries(cleanResult).map(([k, v]) => (
            <p key={k} className="text-farm-400 text-xs">{k}: {v} records deleted</p>
          ))}
        </div>
      )}
    </div>
  )
}
