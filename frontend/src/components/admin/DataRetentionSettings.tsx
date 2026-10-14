import { useState, useEffect, useRef } from 'react'
import { Clock, Save, Loader2, Trash2, AlertTriangle } from 'lucide-react'
import { retention } from '../../api'

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

  const [loadAttempt, setLoadAttempt] = useState(0)
  const [persisted, setPersisted] = useState(null)
  const [uncertain, setUncertain] = useState(false)
  const operation = useRef(false)
  const mounted = useRef(false)
  const validPolicy = (data) => data && FIELDS.every(({ key }) =>
    typeof data[key] === 'number' && Number.isSafeInteger(data[key]) && data[key] >= 0)
  const draftFor = (data) => Object.fromEntries(FIELDS.map(({ key }) => [key, String(data[key])]))
  const numericDraft = () => {
    if (!FIELDS.every(({ key }) => /^\d+$/.test(config[key] ?? '') && Number.isSafeInteger(Number(config[key])))) return null
    return Object.fromEntries(FIELDS.map(({ key }) => [key, Number(config[key])]))
  }
  const draft = numericDraft()
  const dirty = !persisted || !draft || FIELDS.some(({ key }) => draft[key] !== persisted[key])

  useEffect(() => {
    let active = true
    mounted.current = true
    setLoading(true)
    setError('')
    retention.get().then(data => {
      if (!active) return
      if (!validPolicy(data)) throw new Error('Invalid configuration')
      setPersisted(data)
      setConfig(draftFor(data))
      setUncertain(false)
      setLoading(false)
    }).catch(() => {
      if (!active) return
      setPersisted(null)
      setError('Unable to load data retention configuration')
      setLoading(false)
    })
    return () => { active = false; mounted.current = false }
  }, [loadAttempt])

  const handleSave = async () => {
    if (operation.current || !persisted) return
    const values = numericDraft()
    if (!values) { setError('Enter a nonnegative whole number within the safe range for every field'); return }
    operation.current = true
    setSaving(true)
    setError('')
    setSuccess('')
    setCleanResult(null)
    try {
      const data = await retention.set(values)
      if (!mounted.current) return
      if (!validPolicy(data) || FIELDS.some(({ key }) => data[key] !== values[key])) throw new Error('Unable to verify saved retention policy')
      setPersisted(data)
      setConfig(draftFor(data))
      setUncertain(false)
      setSuccess('Retention policy saved')
    } catch (err) {
      if (mounted.current) { setError(err.message || 'Unable to save retention policy'); setUncertain(true) }
    } finally {
      operation.current = false
      if (mounted.current) setSaving(false)
    }
  }

  const handleCleanup = async () => {
    if (operation.current || !persisted || dirty || uncertain) return
    operation.current = true
    const observed = { ...persisted }
    setCleaning(true)
    setError('')
    setCleanResult(null)
    try {
      const periods = FIELDS.map(({ key, label }) => `${label}: ${observed[key]} days`).join(', ')
      if (!confirm(`Run data cleanup now? This permanently deletes data using the server's current policy. Last observed policy: ${periods}. Another administrator may have changed it.`)) return
      if (!mounted.current || uncertain || !validPolicy(observed) || dirty) return
      const result = await retention.runCleanup()
      if (!mounted.current) return
      if (!result?.deleted || typeof result.deleted !== 'object' || Array.isArray(result.deleted) || !Object.values(result.deleted).every(value => Number.isSafeInteger(value) && value >= 0)) throw new Error('Unable to verify cleanup result')
      setCleanResult(result.deleted)
    } catch (err) {
      if (mounted.current) setError(err.message || 'Unable to verify data cleanup')
    } finally {
      operation.current = false
      if (mounted.current) setCleaning(false)
    }
  }

  if (loading) return <p role="status">Loading data retention…</p>
  if (!persisted) return <div><p role="alert">{error}</p><button onClick={() => setLoadAttempt(value => value + 1)}>Retry loading data retention</button></div>

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <Clock size={20} className="text-[var(--brand-primary)]" />
        <h3 className="text-lg font-semibold">Data Retention</h3>
      </div>
      <p className="text-sm text-[var(--brand-text-secondary)] mb-4">Configure how long data is kept before automatic cleanup. Set to 0 to keep forever.</p>

      {error && (
        <div role="alert" className="mb-3 p-3 bg-red-900/30 border border-red-700 rounded-md flex items-center gap-2 text-sm text-red-400">
          <AlertTriangle size={16} /> {error}
        </div>
      )}
      {success && (
        <div role="status" className="mb-3 p-3 bg-green-900/30 border border-green-700 rounded-md text-sm text-green-400">{success}</div>
      )}

      <fieldset disabled={saving || cleaning} className="space-y-3 mb-4">
        {FIELDS.map(f => (
          <div key={f.key} className="flex items-center gap-3">
            <label htmlFor={`retention-${f.key}`} className="w-40 text-sm text-[var(--brand-text-secondary)]">{f.label}</label>
            <input id={`retention-${f.key}`} type="number" min="0" step="1" className="w-24 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded px-3 py-1.5 text-sm"
              value={config[f.key] ?? ''} placeholder="0"
              onChange={e => { if (!operation.current) { setConfig(prev => ({ ...prev, [f.key]: e.target.value })); setSuccess(''); setCleanResult(null) } }} />
            <span className="text-xs text-[var(--brand-text-muted)]">days</span>
            <span className="text-xs text-[var(--brand-text-muted)]">{f.desc}</span>
          </div>
        ))}
      </fieldset>

      {dirty && <p>Save policy changes before running cleanup.</p>}
      {uncertain && <p role="status">Save outcome is uncertain. Save again or reload the saved policy before cleanup.</p>}
      {uncertain && <button disabled={saving || cleaning} onClick={() => setLoadAttempt(value => value + 1)}>Reload saved policy</button>}
      <div className="flex gap-3">
        <button onClick={handleSave} disabled={saving || cleaning}
          className="flex items-center gap-2 px-4 py-2 rounded bg-[var(--brand-primary)] hover:bg-[var(--brand-primary)] text-white text-sm disabled:opacity-50">
          {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
          Save Policy
        </button>
        <button onClick={handleCleanup} disabled={saving || cleaning || dirty || uncertain}
          className="flex items-center gap-2 px-4 py-2 rounded bg-orange-600/20 hover:bg-orange-600/30 text-orange-400 text-sm border border-orange-700 disabled:opacity-50">
          {cleaning ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
          Run Cleanup Now
        </button>
      </div>

      {cleanResult && (
        <div className="mt-3 p-3 bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] text-sm">
          <p className="text-[var(--brand-text-secondary)] font-medium mb-1">Cleanup complete:</p>
          {Object.entries(cleanResult).map(([k, v]) => (
            <p key={k} className="text-[var(--brand-text-secondary)] text-xs">{k}: {v} records deleted</p>
          ))}
        </div>
      )}
    </div>
  )
}
