import { useState, useEffect } from 'react'
import { Monitor, Smartphone, Globe, Trash2, LogOut, Loader2, CheckCircle } from 'lucide-react'
import { sessions } from '../../api'

function parseUA(ua) {
  if (!ua) return { device: 'Unknown', browser: 'Unknown' }
  const isMobile = /Mobile|Android|iPhone/i.test(ua)
  let browser = 'Unknown'
  if (ua.includes('Firefox')) browser = 'Firefox'
  else if (ua.includes('Edg/')) browser = 'Edge'
  else if (ua.includes('Chrome')) browser = 'Chrome'
  else if (ua.includes('Safari')) browser = 'Safari'
  return { device: isMobile ? 'Mobile' : 'Desktop', browser }
}

export default function SessionManager() {
  const [sessionList, setSessionList] = useState([])
  const [loading, setLoading] = useState(true)
  const [revoking, setRevoking] = useState(null)

  const load = async () => {
    try {
      const data = await sessions.list()
      setSessionList(data)
    } catch { /* ignore */ }
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  const handleRevoke = async (id) => {
    setRevoking(id)
    try {
      await sessions.revoke(id)
      setSessionList(prev => prev.filter(s => s.id !== id))
    } catch { /* ignore */ }
    setRevoking(null)
  }

  const handleRevokeAll = async () => {
    if (!confirm('Sign out of all other sessions?')) return
    setRevoking('all')
    try {
      await sessions.revokeAll()
      await load()
    } catch { /* ignore */ }
    setRevoking(null)
  }

  if (loading) return null

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <Globe size={20} className="text-print-400" />
          <h3 className="text-lg font-semibold">Active Sessions</h3>
        </div>
        {sessionList.length > 1 && (
          <button
            onClick={handleRevokeAll}
            disabled={revoking === 'all'}
            className="flex items-center gap-2 px-3 py-1.5 rounded bg-red-600/20 hover:bg-red-600/30 text-red-400 text-sm border border-red-700"
          >
            <LogOut size={14} />
            {revoking === 'all' ? 'Signing out...' : 'Sign out everywhere else'}
          </button>
        )}
      </div>

      {sessionList.length === 0 ? (
        <p className="text-sm text-farm-500 italic">No active sessions.</p>
      ) : (
        <div className="space-y-2">
          {sessionList.map(s => {
            const { device, browser } = parseUA(s.user_agent)
            const DeviceIcon = device === 'Mobile' ? Smartphone : Monitor
            return (
              <div key={s.id} className="flex items-center justify-between p-3 bg-farm-900 rounded-lg border border-farm-700">
                <div className="flex items-center gap-3">
                  <DeviceIcon size={18} className="text-farm-400" />
                  <div>
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium">{browser} on {device}</span>
                      {s.is_current && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-green-900/40 text-green-400 border border-green-700">
                          This device
                        </span>
                      )}
                    </div>
                    <div className="text-[11px] text-farm-500 mt-0.5">
                      {s.ip_address} &middot; Last seen {s.last_seen_at ? new Date(s.last_seen_at).toLocaleString() : 'recently'}
                    </div>
                  </div>
                </div>
                {!s.is_current && (
                  <button
                    onClick={() => handleRevoke(s.id)}
                    disabled={revoking === s.id}
                    className="p-2 rounded hover:bg-red-900/30 text-farm-500 hover:text-red-400"
                  >
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
