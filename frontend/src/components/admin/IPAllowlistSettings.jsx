import { useState, useEffect } from 'react'
import { Shield, Plus, Trash2, Save, Loader2, AlertTriangle } from 'lucide-react'
import { ipAllowlist } from '../../api'

export default function IPAllowlistSettings() {
  const [config, setConfig] = useState({ enabled: false, cidrs: [], mode: 'api_and_ui' })
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [newCidr, setNewCidr] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  useEffect(() => {
    ipAllowlist.get().then(data => {
      setConfig(data)
      setLoading(false)
    }).catch(() => setLoading(false))
  }, [])

  const addCidr = () => {
    const cidr = newCidr.trim()
    if (!cidr) return
    // Basic validation
    if (!cidr.match(/^[\d.:a-fA-F]+\/?\d*$/)) {
      setError('Invalid IP or CIDR format')
      return
    }
    if (!cidr.includes('/')) {
      setConfig(prev => ({ ...prev, cidrs: [...prev.cidrs, cidr + '/32'] }))
    } else {
      setConfig(prev => ({ ...prev, cidrs: [...prev.cidrs, cidr] }))
    }
    setNewCidr('')
    setError('')
  }

  const removeCidr = (idx) => {
    setConfig(prev => ({ ...prev, cidrs: prev.cidrs.filter((_, i) => i !== idx) }))
  }

  const handleSave = async () => {
    setSaving(true)
    setError('')
    setSuccess('')
    try {
      const data = await ipAllowlist.set(config)
      setConfig(data)
      setSuccess('IP allowlist saved')
      setTimeout(() => setSuccess(''), 3000)
    } catch (err) {
      setError(err.message)
    }
    setSaving(false)
  }

  if (loading) return null

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <Shield size={20} className="text-print-400" />
        <h3 className="text-lg font-semibold">IP Allowlist</h3>
      </div>
      <p className="text-sm text-farm-400 mb-4">Restrict API access to specific IP addresses or CIDR ranges.</p>

      {error && (
        <div className="mb-3 p-3 bg-red-900/30 border border-red-700 rounded-lg flex items-center gap-2 text-sm text-red-400">
          <AlertTriangle size={16} /> {error}
        </div>
      )}
      {success && (
        <div className="mb-3 p-3 bg-green-900/30 border border-green-700 rounded-lg text-sm text-green-400">{success}</div>
      )}

      <div className="space-y-4">
        <label className="flex items-center gap-3 cursor-pointer">
          <input type="checkbox" checked={config.enabled}
            onChange={e => setConfig(prev => ({ ...prev, enabled: e.target.checked }))}
            className="rounded" />
          <span className="text-sm">Enable IP allowlisting</span>
        </label>

        {config.enabled && (
          <>
            <div>
              <label className="block text-sm text-farm-400 mb-1">Allowed IPs / CIDRs</label>
              <div className="space-y-1 mb-2">
                {config.cidrs.map((cidr, i) => (
                  <div key={i} className="flex items-center gap-2">
                    <code className="flex-1 text-xs bg-farm-800 px-3 py-1.5 rounded font-mono text-farm-300">{cidr}</code>
                    <button onClick={() => removeCidr(i)} className="p-1 rounded hover:bg-red-900/30 text-farm-500 hover:text-red-400">
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))}
              </div>
              <div className="flex gap-2">
                <input type="text" value={newCidr} onChange={e => setNewCidr(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && (e.preventDefault(), addCidr())}
                  className="flex-1 bg-farm-800 border border-farm-700 rounded px-3 py-1.5 text-sm font-mono"
                  placeholder="e.g. 192.168.1.0/24 or 10.0.0.5" />
                <button onClick={addCidr}
                  className="flex items-center gap-1 px-3 py-1.5 rounded bg-farm-800 hover:bg-farm-700 text-farm-300 text-sm">
                  <Plus size={14} /> Add
                </button>
              </div>
            </div>
          </>
        )}

        <button onClick={handleSave} disabled={saving}
          className="flex items-center gap-2 px-4 py-2 rounded bg-print-600 hover:bg-print-700 text-white text-sm disabled:opacity-50">
          {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
          Save
        </button>
      </div>
    </div>
  )
}
