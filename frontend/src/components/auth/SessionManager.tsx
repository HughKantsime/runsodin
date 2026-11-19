import { useState, useEffect } from 'react'
import { Monitor, Smartphone, Globe, Trash2, LogOut, Loader2 } from 'lucide-react'
import type { Session, AdminSession } from '../../types'
import { clearSensitiveBrowserState } from '../../permissions'
import { sessions } from '../../api'
import { parseServerDate } from '../../utils/shared'

function parseUA(ua: string | null): { device: string; browser: string } {
  if (!ua) return { device: 'Unknown', browser: 'Unknown' }
  const isMobile = /Mobile|Android|iPhone/i.test(ua)
  let browser = 'Unknown'
  if (ua.includes('Firefox')) browser = 'Firefox'
  else if (ua.includes('Edg/')) browser = 'Edge'
  else if (ua.includes('Chrome')) browser = 'Chrome'
  else if (ua.includes('Safari')) browser = 'Safari'
  return { device: isMobile ? 'Mobile' : 'Desktop', browser }
}

export default function SessionManager({ admin = false }: { admin?: boolean } = {}) {
  const [sessionList, setSessionList] = useState<(Session | (AdminSession & {is_current: boolean}))[]>([])
  const [loading, setLoading] = useState(true)
  const [revoking, setRevoking] = useState<number | 'all' | null>(null)
  const [error, setError] = useState('')

  const load = async () => {
    setError('')
    setLoading(true)
    try {
      if (admin) {
        const [rows, own] = await Promise.all([sessions.adminList(), sessions.list()])
        const currentId = own.find(session => session.is_current)?.id
        setSessionList(rows.map(row => ({...row, is_current: row.id === currentId})))
      } else {
        setSessionList(await sessions.list())
      }
    } catch (err) { setError(err instanceof Error ? err.message : 'Unable to load sessions.') }
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  const handleRevoke = async (id: number, current = false) => {
    if (admin && !confirm(current ? 'Ending this session signs you out. Continue?' : 'End this session? The user will need to sign in again.')) return
    setError('')
    setRevoking(id)
    try {
      if (admin) await sessions.adminRevoke(id)
      else await sessions.revoke(id)
      if (current) {
        try { await clearSensitiveBrowserState() } finally { window.location.href = '/login' }
        return
      }
      setSessionList(prev => prev.filter(s => s.id !== id))
    } catch (err) { setError(err instanceof Error ? err.message : 'Unable to end the session.') }
    setRevoking(null)
  }

  const handleRevokeAll = async () => {
    if (!confirm('Sign out of all other sessions?')) return
    setRevoking('all')
    try {
      await sessions.revokeAll()
      await load()
    } catch (err) { setError(err instanceof Error ? err.message : 'Unable to end the session.') }
    setRevoking(null)
  }

  if (loading) return <p role="status">Loading sessions...</p>

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-2 mb-4">
        <div className="flex items-center gap-3">
          <Globe size={20} className="text-[var(--brand-primary)]" />
          <h3 className="text-lg font-semibold">Active Sessions</h3>
        </div>
        {!admin && sessionList.length > 1 && (
          <button
            onClick={handleRevokeAll}
            disabled={revoking !== null}
            className="flex items-center gap-2 px-3 py-1.5 rounded bg-red-600/20 hover:bg-red-600/30 text-red-400 text-sm border border-red-700"
          >
            <LogOut size={14} />
            {revoking === 'all' ? 'Signing out...' : 'Sign out everywhere else'}
          </button>
        )}
      </div>

      {error && <div role="alert" className="mb-3">{error} <button onClick={load} className="underline">Reload sessions</button></div>}
      {sessionList.length === 0 ? (
        !error && <p className="text-sm text-[var(--brand-text-muted)] italic">No active sessions.</p>
      ) : (
        <div className="space-y-2">
          {sessionList.map(s => {
            const { device, browser } = parseUA(s.user_agent)
            const DeviceIcon = device === 'Mobile' ? Smartphone : Monitor
            return (
              <div key={s.id} className="flex items-center justify-between flex-wrap gap-2 p-3 bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)]">
                <div className="flex min-w-0 items-center gap-3 break-all">
                  <DeviceIcon size={18} className="text-[var(--brand-text-secondary)]" />
                  <div>
                    {'username' in s && <p className="font-semibold">{s.username}</p>}
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium">{browser} on {device}</span>
                      {s.is_current && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-green-900/40 text-green-400 border border-green-700">
                          This device
                        </span>
                      )}
                    </div>
                    <div className="text-[11px] text-[var(--brand-text-muted)] mt-0.5">
                      {s.ip_address || 'Unknown address'} &middot; Last seen {s.last_seen_at ? parseServerDate(s.last_seen_at).toLocaleString() : 'unknown'}
                    </div>
                  </div>
                </div>
                {'username' in s ? (
                  <button aria-label={`End session for ${s.username}`} onClick={() => handleRevoke(s.id, s.is_current)}
                    disabled={revoking !== null} className="min-h-11 p-2 underline">
                    {revoking === s.id ? 'Ending session...' : 'End session'}
                  </button>
                ) : !s.is_current && (
                  <button aria-label="Revoke session" onClick={() => handleRevoke(s.id)}
                    disabled={revoking !== null}
                    className="min-h-11 min-w-11 p-2 rounded hover:bg-red-900/30 text-[var(--brand-text-muted)] hover:text-red-400">
                    {revoking === s.id ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}
                  </button>
                )}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
