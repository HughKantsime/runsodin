import { useState, useEffect } from 'react'
import { Key, Plus, Trash2, Copy, CheckCircle, AlertTriangle, Clock, Loader2 } from 'lucide-react'
import { apiTokens } from '../../api'

const AVAILABLE_SCOPES = [
  { value: 'read:printers', label: 'Read Printers' },
  { value: 'write:printers', label: 'Write Printers' },
  { value: 'read:jobs', label: 'Read Jobs' },
  { value: 'write:jobs', label: 'Write Jobs' },
  { value: 'read:spools', label: 'Read Spools' },
  { value: 'write:spools', label: 'Write Spools' },
  { value: 'read:models', label: 'Read Models' },
  { value: 'write:models', label: 'Write Models' },
  { value: 'read:analytics', label: 'Read Analytics' },
  { value: 'admin', label: 'Full Admin' },
]

export default function APITokenManager() {
  const [tokens, setTokens] = useState([])
  const [loading, setLoading] = useState(true)
  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const [scopes, setScopes] = useState([])
  const [expiresDays, setExpiresDays] = useState('')
  const [newToken, setNewToken] = useState(null)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')
  const [actionLoading, setActionLoading] = useState(false)

  const loadTokens = async () => {
    try {
      const data = await apiTokens.list()
      setTokens(data)
    } catch { /* ignore */ }
    setLoading(false)
  }

  useEffect(() => { loadTokens() }, [])

  const handleCreate = async (e) => {
    e.preventDefault()
    setError('')
    setActionLoading(true)
    try {
      const data = await apiTokens.create({
        name,
        scopes,
        expires_days: expiresDays ? parseInt(expiresDays) : null,
      })
      setNewToken(data.token)
      setName('')
      setScopes([])
      setExpiresDays('')
      setShowCreate(false)
      loadTokens()
    } catch (err) {
      setError(err.message || 'Failed to create token')
    } finally {
      setActionLoading(false)
    }
  }

  const handleRevoke = async (id) => {
    if (!confirm('Revoke this token? This cannot be undone.')) return
    try {
      await apiTokens.revoke(id)
      setTokens(tokens.filter(t => t.id !== id))
    } catch { /* ignore */ }
  }

  const copyToken = () => {
    navigator.clipboard.writeText(newToken)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  const toggleScope = (scope) => {
    setScopes(prev => prev.includes(scope) ? prev.filter(s => s !== scope) : [...prev, scope])
  }

  if (loading) return null

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <Key size={20} className="text-[var(--brand-primary)]" />
          <h3 className="text-lg font-semibold">API Tokens</h3>
        </div>
        {!showCreate && !newToken && (
          <button
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-2 px-3 py-1.5 rounded bg-[var(--brand-primary)] hover:opacity-90 text-white text-sm"
          >
            <Plus size={14} /> New Token
          </button>
        )}
      </div>

      <p className="text-sm text-[var(--brand-text-secondary)] mb-4">
        Create personal API tokens with specific permissions for scripts, integrations, and CI/CD.
      </p>

      {error && (
        <div className="mb-4 p-3 bg-red-900/30 border border-red-700 rounded-md flex items-center gap-2 text-sm text-red-400">
          <AlertTriangle size={16} /> {error}
        </div>
      )}

      {/* New token reveal */}
      {newToken && (
        <div className="mb-4 p-4 bg-green-900/20 border border-green-700 rounded-md">
          <p className="text-sm text-green-400 mb-2 font-medium">Token created. Copy it now — it won't be shown again.</p>
          <div className="flex items-center gap-2">
            <code className="flex-1 text-xs bg-[var(--brand-card-bg)] px-3 py-2 rounded font-mono text-[var(--brand-text-primary)] select-all break-all">
              {newToken}
            </code>
            <button onClick={copyToken} className="p-2 rounded hover:bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)] shrink-0">
              {copied ? <CheckCircle size={16} className="text-green-400" /> : <Copy size={16} />}
            </button>
          </div>
          <button onClick={() => setNewToken(null)} className="mt-2 text-xs text-[var(--brand-text-muted)] hover:text-[var(--brand-text-secondary)]">
            Dismiss
          </button>
        </div>
      )}

      {/* Create form */}
      {showCreate && (
        <form onSubmit={handleCreate} className="mb-4 p-4 bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] space-y-4">
          <div>
            <label className="block text-sm text-[var(--brand-text-secondary)] mb-1">Token name</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded px-3 py-2 text-sm"
              placeholder="e.g. CI Pipeline, Monitoring Script"
              required
              autoFocus
            />
          </div>
          <div>
            <label className="block text-sm text-[var(--brand-text-secondary)] mb-1">Scopes</label>
            <div className="flex flex-wrap gap-2">
              {AVAILABLE_SCOPES.map(s => (
                <button
                  key={s.value}
                  type="button"
                  onClick={() => toggleScope(s.value)}
                  className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
                    scopes.includes(s.value)
                      ? 'bg-[var(--brand-primary)] text-white'
                      : 'bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)] hover:opacity-80'
                  }`}
                >
                  {s.label}
                </button>
              ))}
            </div>
          </div>
          <div>
            <label className="block text-sm text-[var(--brand-text-secondary)] mb-1">Expires in (days, blank = never)</label>
            <input
              type="number"
              min="1"
              max="365"
              value={expiresDays}
              onChange={(e) => setExpiresDays(e.target.value)}
              className="w-32 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded px-3 py-2 text-sm"
              placeholder="Never"
            />
          </div>
          <div className="flex gap-2">
            <button
              type="submit"
              disabled={actionLoading || !name.trim()}
              className="flex items-center gap-2 px-4 py-2 rounded bg-[var(--brand-primary)] hover:opacity-90 text-white text-sm disabled:opacity-50"
            >
              {actionLoading ? <Loader2 size={14} className="animate-spin" /> : <Key size={14} />}
              Create Token
            </button>
            <button
              type="button"
              onClick={() => { setShowCreate(false); setError('') }}
              className="px-4 py-2 rounded bg-[var(--brand-input-bg)] hover:bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)] text-sm"
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {/* Token list */}
      {tokens.length > 0 ? (
        <div className="space-y-2">
          {tokens.map(t => (
            <div key={t.id} className="flex items-center justify-between p-3 bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)]">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-1">
                  <span className="font-medium text-sm">{t.name}</span>
                  <code className="text-xs text-[var(--brand-text-muted)] font-mono">{t.prefix}...</code>
                </div>
                <div className="flex flex-wrap gap-1">
                  {(t.scopes || []).map(s => (
                    <span key={s} className="text-xs px-1.5 py-0.5 rounded bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)]">{s}</span>
                  ))}
                  {(!t.scopes || t.scopes.length === 0) && (
                    <span className="text-xs text-[var(--brand-text-muted)] italic">No scopes (global)</span>
                  )}
                </div>
                <div className="flex gap-3 mt-1 text-xs text-[var(--brand-text-muted)]">
                  {t.expires_at && (
                    <span className="flex items-center gap-1">
                      <Clock size={10} /> Expires {new Date(t.expires_at).toLocaleDateString()}
                    </span>
                  )}
                  {t.last_used_at && (
                    <span>Last used {new Date(t.last_used_at).toLocaleDateString()}</span>
                  )}
                </div>
              </div>
              <button
                onClick={() => handleRevoke(t.id)}
                className="p-2 rounded hover:bg-red-900/30 text-[var(--brand-text-muted)] hover:text-red-400"
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      ) : !showCreate && (
        <p className="text-sm text-[var(--brand-text-muted)] italic">No API tokens created yet.</p>
      )}
    </div>
  )
}
