import { useState, useEffect } from 'react'
import { X } from 'lucide-react'
import { printers, printerTelemetry } from '../../api'

interface HmsEntry {
  id: number
  code: string
  message: string | null
  severity: string
  occurred_at: string
  [key: string]: any
}

interface HmsData {
  entries: HmsEntry[]
  frequency: Record<string, number>
  total: number
}

const SEVERITY_COLORS: Record<string, string> = {
  critical: 'bg-red-600/10 text-red-400/80',
  error: 'bg-orange-600/10 text-orange-400/80',
  warning: 'bg-yellow-600/10 text-yellow-400/80',
  info: 'bg-blue-600/10 text-blue-400/80',
}

interface HmsHistoryPanelProps {
  printerId: number
  apiType?: string | null
  onClose?: () => void
}

export default function HmsHistoryPanel({ printerId, apiType, onClose }: HmsHistoryPanelProps) {
  const [data, setData] = useState<HmsData>({ entries: [], frequency: {}, total: 0 })
  const [days, setDays] = useState(30)
  const [loading, setLoading] = useState(true)
  const [clearing, setClearing] = useState(false)

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

  const formatDate = (d: string) => new Date(d).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })

  // Top recurring errors
  const topErrors = Object.entries(data.frequency || {})
    .sort(([, a], [, b]) => b - a)
    .slice(0, 5)

  return (
    <div className="rounded-md p-4" style={{ backgroundColor: 'var(--brand-card-bg)', border: '1px solid var(--brand-sidebar-border)' }}>
      <div className="flex items-center justify-between mb-3">
        <h4 className="text-sm font-semibold" style={{ color: 'var(--brand-text-primary)' }}>HMS Error History</h4>
        <div className="flex items-center gap-2">
          {[7, 30, 90].map(d => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`px-2 py-1 rounded-md text-xs transition-colors ${days === d ? 'bg-[var(--brand-primary)] text-white' : 'bg-[var(--brand-card-bg)] text-[var(--brand-text-secondary)] hover:bg-[var(--brand-card-border)]'}`}
            >
              {d}d
            </button>
          ))}
          {apiType === 'bambu' && data.total > 0 && (
            <button
              onClick={async () => {
                setClearing(true)
                try { await printers.clearErrors(printerId); loadData() } catch {}
                setClearing(false)
              }}
              disabled={clearing}
              className="px-2 py-1 rounded-md text-xs bg-red-600/20 text-red-400 hover:bg-red-600/30 transition-colors disabled:opacity-50"
            >
              {clearing ? 'Clearing...' : 'Clear Errors'}
            </button>
          )}
          {onClose && (
            <button onClick={onClose} className="ml-2 text-[var(--brand-text-muted)] hover:text-[var(--brand-text-primary)] text-xs"><X size={12} /></button>
          )}
        </div>
      </div>

      {loading ? (
        <div className="text-center py-8 text-[var(--brand-text-muted)] text-sm">Loading...</div>
      ) : data.total === 0 ? (
        <div className="text-center py-8 text-[var(--brand-text-muted)] text-sm">No HMS errors in the last {days} days</div>
      ) : (
        <div className="space-y-3">
          {/* Frequency summary */}
          {topErrors.length > 0 && (
            <div>
              <span className="text-xs text-[var(--brand-text-muted)] font-semibold">Top Recurring ({data.total} total)</span>
              <div className="mt-1 space-y-1">
                {topErrors.map(([code, count]) => {
                  const entry = data.entries.find(e => e.code === code)
                  return (
                    <div key={code} className="flex items-center justify-between text-xs py-1">
                      <span className="text-[var(--brand-text-secondary)] truncate mr-2">{entry?.message || code}</span>
                      <span className="text-[var(--brand-text-muted)] shrink-0">{count}x</span>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* Recent entries */}
          <div>
            <span className="text-xs text-[var(--brand-text-muted)] font-semibold">Recent Errors</span>
            <div className="mt-1 space-y-1 max-h-48 overflow-y-auto">
              {data.entries.slice(0, 20).map(e => (
                <div key={e.id} className="flex items-center gap-2 text-xs py-1 border-b border-[var(--brand-card-border)]">
                  <span className={`px-1.5 py-0.5 rounded-md text-[10px] ${SEVERITY_COLORS[e.severity] || SEVERITY_COLORS.info}`}>
                    {e.severity}
                  </span>
                  <span className="text-[var(--brand-text-secondary)] truncate flex-1">{e.message || e.code}</span>
                  <span className="text-[var(--brand-text-muted)] shrink-0">{formatDate(e.occurred_at)}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
