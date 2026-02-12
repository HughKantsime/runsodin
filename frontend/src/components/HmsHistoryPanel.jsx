import { useState, useEffect } from 'react'
import { printerTelemetry } from '../api'

const SEVERITY_COLORS = {
  critical: 'bg-red-600/20 text-red-400',
  error: 'bg-orange-600/20 text-orange-400',
  warning: 'bg-yellow-600/20 text-yellow-400',
  info: 'bg-blue-600/20 text-blue-400',
}

export default function HmsHistoryPanel({ printerId, onClose }) {
  const [data, setData] = useState({ entries: [], frequency: {}, total: 0 })
  const [days, setDays] = useState(30)
  const [loading, setLoading] = useState(true)

  useEffect(() => { loadData() }, [printerId, days])

  const loadData = async () => {
    setLoading(true)
    try {
      const result = await printerTelemetry.hmsHistory(printerId, days)
      setData(result || { entries: [], frequency: {}, total: 0 })
    } catch (err) {
      console.error('Failed to load HMS history:', err)
    } finally {
      setLoading(false)
    }
  }

  const formatDate = (d) => new Date(d).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })

  // Top recurring errors
  const topErrors = Object.entries(data.frequency || {})
    .sort(([, a], [, b]) => b - a)
    .slice(0, 5)

  return (
    <div className="rounded-lg p-4" style={{ backgroundColor: 'var(--brand-card-bg)', border: '1px solid var(--brand-sidebar-border)' }}>
      <div className="flex items-center justify-between mb-3">
        <h4 className="text-sm font-semibold" style={{ color: 'var(--brand-text-primary)' }}>HMS Error History</h4>
        <div className="flex items-center gap-2">
          {[7, 30, 90].map(d => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`px-2 py-1 rounded-lg text-xs transition-colors ${days === d ? 'bg-print-600 text-white' : 'bg-farm-800 text-farm-400 hover:bg-farm-700'}`}
            >
              {d}d
            </button>
          ))}
          {onClose && (
            <button onClick={onClose} className="ml-2 text-farm-500 hover:text-farm-300 text-xs">✕</button>
          )}
        </div>
      </div>

      {loading ? (
        <div className="text-center py-8 text-farm-500 text-sm">Loading...</div>
      ) : data.total === 0 ? (
        <div className="text-center py-8 text-farm-500 text-sm">No HMS errors in the last {days} days</div>
      ) : (
        <div className="space-y-3">
          {/* Frequency summary */}
          {topErrors.length > 0 && (
            <div>
              <span className="text-xs text-farm-500 font-semibold">Top Recurring ({data.total} total)</span>
              <div className="mt-1 space-y-1">
                {topErrors.map(([code, count]) => {
                  const entry = data.entries.find(e => e.code === code)
                  return (
                    <div key={code} className="flex items-center justify-between text-xs py-1">
                      <span className="text-farm-300 truncate mr-2">{entry?.message || code}</span>
                      <span className="text-farm-500 shrink-0">{count}x</span>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Recent entries */}
          <div>
            <span className="text-xs text-farm-500 font-semibold">Recent Errors</span>
            <div className="mt-1 space-y-1 max-h-48 overflow-y-auto">
              {data.entries.slice(0, 20).map(e => (
                <div key={e.id} className="flex items-center gap-2 text-xs py-1 border-b border-farm-800">
                  <span className={`px-1.5 py-0.5 rounded-lg text-[10px] ${SEVERITY_COLORS[e.severity] || SEVERITY_COLORS.info}`}>
                    {e.severity}
                  </span>
                  <span className="text-farm-300 truncate flex-1">{e.message || e.code}</span>
                  <span className="text-farm-600 shrink-0">{formatDate(e.occurred_at)}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
