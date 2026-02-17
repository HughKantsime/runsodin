import { useState, useEffect } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Bell, Check, X, ExternalLink, CheckCircle, XCircle } from 'lucide-react'
import toast from 'react-hot-toast'
import { alerts as alertsApi, approveJob, rejectJob } from '../api'

const SEVERITY_STYLES = {
  critical: { bg: 'border-l-red-500', icon: '\u{1F534}', label: 'Critical' },
  warning: { bg: 'border-l-amber-500', icon: '\u{1F7E1}', label: 'Warning' },
  info: { bg: 'border-l-green-500', icon: '\u{1F7E2}', label: 'Info' },
}

const TABS = [
  { key: 'all', label: 'All' },
  { key: 'unread', label: 'Unread' },
  { key: 'critical', label: 'Critical' },
  { key: 'warning', label: 'Warning' },
  { key: 'info', label: 'Info' },
]

function formatDate(dateStr) {
  const d = new Date(dateStr)
  return d.toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: 'numeric', minute: '2-digit',
  })
}

export default function Alerts() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [alerts, setAlerts] = useState([])
  const [rejectingAlertJobId, setRejectingAlertJobId] = useState(null)
  const [rejectReason, setRejectReason] = useState('')
  const [actionLoading, setActionLoading] = useState(null)
  const [loading, setLoading] = useState(true)
  const [activeTab, setActiveTab] = useState(searchParams.get('filter') || 'all')
  const [hasMore, setHasMore] = useState(false)
  const [offset, setOffset] = useState(0)
  const LIMIT = 25

  const loadAlerts = async (reset = false) => {
    try {
      setLoading(true)
      const newOffset = reset ? 0 : offset
      const params = { limit: LIMIT, offset: newOffset }

      if (activeTab === 'unread') {
        params.is_read = false
      } else if (['critical', 'warning', 'info'].includes(activeTab)) {
        params.severity = activeTab
      }

      const data = await alertsApi.list(params)
      if (reset) {
        setAlerts(data)
        setOffset(LIMIT)
      } else {
        setAlerts(prev => [...prev, ...data])
        setOffset(prev => prev + LIMIT)
      }
      setHasMore(data.length === LIMIT)
    } catch (err) {
      console.error('Failed to load alerts:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadAlerts(true)
  }, [activeTab])

  const handleTabChange = (tab) => {
    setActiveTab(tab)
    setSearchParams(tab === 'all' ? {} : { filter: tab })
  }

  const handleMarkRead = async (alertId) => {
    try {
      await alertsApi.markRead(alertId)
      setAlerts(prev => prev.map(a => a.id === alertId ? { ...a, is_read: true } : a))
    } catch (err) {
      toast.error('Failed to mark alert as read')
    }
  }

  const handleDismiss = async (alertId) => {
    try {
      await alertsApi.dismiss(alertId)
      setAlerts(prev => prev.filter(a => a.id !== alertId))
    } catch (err) {
      toast.error('Failed to dismiss alert')
    }
  }

  const handleMarkAllRead = async () => {
    try {
      await alertsApi.markAllRead()
      setAlerts(prev => prev.map(a => ({ ...a, is_read: true })))
    } catch (err) {
      toast.error('Failed to mark all alerts as read')
    }
  }

  const getActionLink = (alert) => {
    if (alert.job_id) return { label: 'View Job', path: '/jobs' }
    if (alert.spool_id) return { label: 'View Spool', path: '/spools' }
    if (alert.printer_id) return { label: 'View Printer', path: '/printers' }
    if (alert.alert_type === 'maintenance_overdue') return { label: 'View Maintenance', path: '/maintenance' }
    return null
  }

  const handleApproveFromAlert = async (alert) => {
    if (!alert.job_id) return
    setActionLoading(alert.id)
    try {
      await approveJob(alert.job_id)
      await handleMarkRead(alert.id)
      loadAlerts()
    } catch (err) {
      console.error('Failed to approve job:', err)
    } finally {
      setActionLoading(null)
    }
  }

  const handleRejectFromAlert = async (alert) => {
    if (!rejectReason.trim()) return
    setActionLoading(alert.id)
    try {
      await rejectJob(alert.job_id, rejectReason)
      await handleMarkRead(alert.id)
      setRejectingAlertJobId(null)
      setRejectReason('')
      loadAlerts()
    } catch (err) {
      console.error('Failed to reject job:', err)
    } finally {
      setActionLoading(null)
    }
  }

  const unreadCount = alerts.filter(a => !a.is_read).length

  return (
    <div className="p-4 md:p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <Bell className="text-print-400" size={24} />
          <h1 className="text-xl md:text-2xl font-display font-bold">Alerts</h1>
          {unreadCount > 0 && (
            <span className="bg-red-500/20 text-red-400 text-xs font-medium px-2 py-0.5 rounded-full">
              {unreadCount} unread
            </span>
          )}
        </div>
        {unreadCount > 0 && (
          <button
            onClick={handleMarkAllRead}
            className="px-3 py-1.5 text-xs rounded-lg transition-colors"
            style={{ backgroundColor: 'var(--brand-accent)', color: 'white' }}
          >
            Mark all read
          </button>
        )}
      </div>

      <div className="flex gap-1 mb-6 p-1 rounded-lg" style={{ backgroundColor: 'var(--brand-sidebar-bg)' }}>
        {TABS.map(tab => (
          <button
            key={tab.key}
            onClick={() => handleTabChange(tab.key)}
            className="px-3 py-1.5 text-xs font-medium rounded-md transition-colors"
            style={activeTab === tab.key
              ? { backgroundColor: 'var(--brand-accent)', color: 'white' }
              : { color: 'var(--brand-text-muted)' }
            }
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="space-y-3">
        {loading && alerts.length === 0 ? (
          <div className="text-center py-12" style={{ color: 'var(--brand-text-muted)' }}>Loading alerts...</div>
        ) : alerts.length === 0 ? (
          <div className="text-center py-12 rounded-lg border" style={{
            backgroundColor: 'var(--brand-card-bg)',
            borderColor: 'var(--brand-sidebar-border)',
            color: 'var(--brand-text-muted)',
          }}>
            <Bell size={32} className="mx-auto mb-3 opacity-30" />
            <p className="text-sm">No alerts to show</p>
          </div>
        ) : (
          alerts.map(alert => {
            const sev = SEVERITY_STYLES[alert.severity] || SEVERITY_STYLES.info
            const action = getActionLink(alert)

            return (
              <div
                key={alert.id}
                className={`rounded-lg border border-l-4 ${sev.bg} transition-all ${!alert.is_read ? 'ring-1 ring-print-500/20' : ''}`}
                style={{
                  backgroundColor: 'var(--brand-card-bg)',
                  borderColor: 'var(--brand-sidebar-border)',
                  opacity: alert.is_read ? 0.7 : 1,
                }}
              >
                <div className="p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex gap-3 min-w-0 flex-1">
                      <span className="text-lg flex-shrink-0">{sev.icon}</span>
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-semibold" style={{ color: 'var(--brand-text-primary)' }}>
                          {alert.title}
                        </div>
                        {alert.message && (
                          <p className="text-xs mt-1 leading-relaxed" style={{ color: 'var(--brand-text-secondary)' }}>
                            {alert.message}
                          </p>
                        )}
                        <div className="flex items-center gap-3 mt-2">
                          <span className="text-[10px]" style={{ color: 'var(--brand-text-muted)' }}>
                            {formatDate(alert.created_at)}
                          </span>
                          {alert.printer_name && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded-lg" style={{
                              backgroundColor: 'var(--brand-sidebar-bg)',
                              color: 'var(--brand-text-muted)',
                            }}>
                              {alert.printer_name}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>

                    <div className="flex items-center gap-1 flex-shrink-0">
                      {alert.alert_type === 'job_submitted' && !alert.is_read && alert.job_id && (
                        <>
                          <button
                            onClick={() => handleApproveFromAlert(alert)}
                            disabled={actionLoading === alert.id}
                            className="p-1.5 rounded-lg transition-colors hover:bg-green-900/50 text-green-400"
                            title="Approve Job"
                          >
                            <CheckCircle size={14} />
                          </button>
                          <button
                            onClick={() => setRejectingAlertJobId(rejectingAlertJobId === alert.id ? null : alert.id)}
                            disabled={actionLoading === alert.id}
                            className="p-1.5 rounded-lg transition-colors hover:bg-red-900/50 text-red-400"
                            title="Reject Job"
                          >
                            <XCircle size={14} />
                          </button>
                        </>
                      )}
                      {action && alert.alert_type !== 'job_submitted' && (
                        <a
                          href={action.path}
                          className="p-1.5 rounded-lg transition-colors hover:bg-farm-800 text-farm-400"
                          title={action.label}
                        >
                          <ExternalLink size={14} />
                        </a>
                      )}
                      {!alert.is_read && (
                        <button
                          onClick={() => handleMarkRead(alert.id)}
                          className="p-1.5 rounded-lg transition-colors hover:bg-farm-800 text-farm-400"
                          title="Mark read"
                        >
                          <Check size={14} />
                        </button>
                      )}
                      <button
                        onClick={() => handleDismiss(alert.id)}
                        className="p-1.5 rounded-lg transition-colors hover:bg-red-900/50 text-farm-500 hover:text-red-400"
                        title="Dismiss"
                      >
                        <X size={14} />
                      </button>
                    </div>
                  </div>
                </div>
                  {rejectingAlertJobId === alert.id && (
                    <div className="mt-3 pt-3 px-4 pb-4 border-t border-farm-700">
                      <label className="block text-xs text-farm-400 mb-1">Rejection reason (required):</label>
                      <div className="flex gap-2">
                        <input
                          type="text"
                          value={rejectReason}
                          onChange={(e) => setRejectReason(e.target.value)}
                          placeholder="e.g., Too much filament — please re-slice with 10% infill"
                          className="flex-1 bg-farm-800 border border-farm-700 rounded-lg px-2 py-1.5 text-sm text-white"
                          autoFocus
                          onKeyDown={(e) => e.key === 'Enter' && rejectReason.trim() && handleRejectFromAlert(alert)}
                        />
                        <button
                          onClick={() => handleRejectFromAlert(alert)}
                          disabled={!rejectReason.trim() || actionLoading === alert.id}
                          className="px-3 py-1.5 bg-red-600 hover:bg-red-500 disabled:bg-farm-700 disabled:text-farm-500 rounded-lg text-sm text-white transition-colors"
                        >
                          Reject
                        </button>
                        <button
                          onClick={() => { setRejectingAlertJobId(null); setRejectReason('') }}
                          className="px-2 py-1.5 bg-farm-700 hover:bg-farm-600 rounded-lg text-sm text-farm-300 transition-colors"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}
              </div>
            )
          })
        )}
      </div>

      {hasMore && (
        <div className="text-center mt-6">
          <button
            onClick={() => loadAlerts(false)}
            className="px-4 py-2 text-sm rounded-lg transition-colors"
            style={{ backgroundColor: 'var(--brand-sidebar-bg)', color: 'var(--brand-text-secondary)' }}
            disabled={loading}
          >
            {loading ? 'Loading...' : 'Load more...'}
          </button>
        </div>
      )}
    </div>
  )
}
