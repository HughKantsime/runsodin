import { useState, useEffect, useRef } from 'react'
import { Shield, Plus, Trash2, Save, Loader2, AlertTriangle } from 'lucide-react'
import { ipAllowlist } from '../../api'

export default function IPAllowlistSettings() {
  const [config, setConfig] = useState({ enabled: false, cidrs: [], mode: 'api_and_ui' })
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [newCidr, setNewCidr] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const [loadAttempt, setLoadAttempt] = useState(0)
  const operation = useRef(false)
  const mounted = useRef(false)
  const validConfig = (data) => data && typeof data.enabled === 'boolean' &&
    Array.isArray(data.cidrs) && data.cidrs.every(cidr => typeof cidr === 'string') &&
    typeof data.mode === 'string'

  useEffect(() => {
    let active = true
    mounted.current = true
    setLoading(true)
    setError('')
    ipAllowlist.get().then(data => {
      if (!active) return
      if (!validConfig(data)) throw new Error('Invalid configuration')
      setConfig(data)
      setLoading(false)
    }).catch(() => {
      if (!active) return
      setConfig(null)
      setError('Unable to load IP allowlist configuration')
      setLoading(false)
    })
    return () => { active = false; mounted.current = false }
  }, [loadAttempt])

  const addCidr = () => {
    if (operation.current || !config) return
    const cidr = newCidr.trim()
    if (!cidr) return
    // Basic validation
    if (!cidr.match(/^[\d.:a-fA-F]+\/?\d*$/)) {
      setError('Invalid IP or CIDR format')
      return
    }
    if (!cidr.includes('/')) {
      setConfig(prev => ({ ...prev, cidrs: [...prev.cidrs, cidr + (cidr.includes(':') ? '/128' : '/32')] }))
    } else {
      setConfig(prev => ({ ...prev, cidrs: [...prev.cidrs, cidr] }))
    }
    setNewCidr('')
    setError('')
  }

  const removeCidr = (idx) => {
    if (operation.current || !config) return
    setConfig(prev => ({ ...prev, cidrs: prev.cidrs.filter((_, i) => i !== idx) }))
  }

  const handleSave = async () => {
    if (operation.current || !config) return
    operation.current = true
    setSaving(true)
    setError('')
    setSuccess('')
    try {
      const data = await ipAllowlist.set(config)
      if (!mounted.current) return
      if (!validConfig(data)) throw new Error('Unable to verify saved allowlist configuration')
      setConfig(data)
      setSuccess('IP allowlist saved')

    } catch (err) {
      if (mounted.current) setError(err.message || 'Unable to save IP allowlist')
    }
    operation.current = false
    if (mounted.current) setSaving(false)
  }

  if (loading) return <p role="status">Loading IP allowlist…</p>
  if (!config) return <div><p role="alert">{error}</p><button onClick={() => setLoadAttempt(value => value + 1)}>Retry loading IP allowlist</button></div>

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <Shield size={20} className="text-[var(--brand-primary)]" />
        <h3 className="text-lg font-semibold">IP Allowlist</h3>
      </div>
      <p className="text-sm text-[var(--brand-text-secondary)] mb-4">Restrict API access to specific IP addresses or CIDR ranges.</p>

      {error && (
        <div role="alert" className="mb-3 p-3 bg-red-900/30 border border-red-700 rounded-md flex items-center gap-2 text-sm text-red-400">
          <AlertTriangle size={16} /> {error}
        </div>
      )}
      {success && (
        <div role="status" className="mb-3 p-3 bg-green-900/30 border border-green-700 rounded-md text-sm text-green-400">{success}</div>
      )}

      <fieldset disabled={saving} className="space-y-4">
        <label className="flex items-center gap-3 cursor-pointer">
          <input type="checkbox" checked={config.enabled}
            onChange={e => { if (!operation.current) setConfig(prev => ({ ...prev, enabled: e.target.checked })) }}
            className="rounded" />
          <span className="text-sm">Enable IP allowlisting</span>
        </label>

        {config.enabled && (
          <>
            <div>
              <label className="block text-sm text-[var(--brand-text-secondary)] mb-1">Allowed IPs / CIDRs</label>
              <div className="space-y-1 mb-2">
                {config.cidrs.map((cidr, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <code className="flex-1 text-xs bg-[var(--brand-input-bg)] px-3 py-1.5 rounded font-mono text-[var(--brand-text-secondary)]">{cidr}</code>
                    <button aria-label={`Remove ${cidr}`} onClick={() => removeCidr(i)} className="p-1 rounded hover:bg-red-900/30 text-[var(--brand-text-muted)] hover:text-red-400">
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))}
              </div>
              <div className="flex gap-2">
                <input aria-label="IP address or CIDR" type="text" value={newCidr} onChange={e => setNewCidr(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && (e.preventDefault(), addCidr())}
                  className="flex-1 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded px-3 py-1.5 text-sm font-mono"
                  placeholder="e.g. 192.168.1.0/24 or 10.0.0.5" />
                <button onClick={addCidr}
                  className="flex items-center gap-1 px-3 py-1.5 rounded bg-[var(--brand-input-bg)] hover:bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)] text-sm">
                  <Plus size={14} /> Add
                </button>
              </div>
            </div>
          </>
        )}

        <button onClick={handleSave} disabled={saving}
          className="flex items-center gap-2 px-4 py-2 rounded bg-[var(--brand-primary)] hover:bg-[var(--brand-primary)] text-white text-sm disabled:opacity-50">
          {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
          Save
        </button>
      </fieldset>
    </div>
  )
}
