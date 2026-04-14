import { useState, useEffect, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Bell, Check, X, ExternalLink, CheckCircle, XCircle, AlertOctagon, AlertTriangle, Info, Mail } from 'lucide-react'
import toast from 'react-hot-toast'
import { alerts as alertsApi, approveJob, rejectJob, fetchAPI, printers as printersApi } from '../../api'
import { formatDate } from '../../utils/shared'
import { PageHeader, Button, EmptyState } from '../../components/ui'

interface DigestStatusEntry {
  window_ended_at: string
  status: string
  sent_at: string
  claimed: boolean
}

function DigestStatusStrip() {
  // v1.8.8: surface the last quiet-hours digest delivery outcome so
  // operators see at-a-glance whether last night's digest landed.
  const { data } = useQuery<{ user_digest: DigestStatusEntry | null; org_digest: DigestStatusEntry | null }>({
    queryKey: ['digest-status'],
    queryFn: () => fetchAPI('/alerts/digest-status'),
    refetchInterval: 5 * 60 * 1000,
  })
  if (!data) return null
  const u = data.user_digest
  if (!u) return null
  const isFailed = (u.status || '').startsWith('failed')
  const isSent = u.status === 'sent'
  const tone = isFailed
    ? 'border-red-500/40 bg-red-500/10 text-red-200'
    : isSent
    ? 'border-emerald-500/30 bg-emerald-500/5 text-emerald-200'
    : 'border-amber-500/30 bg-amber-500/5 text-amber-200'
  const label = isFailed
    ? `Last digest (${formatDate(u.window_ended_at)}) FAILED — ${u.status.replace('failed:', '')}`
    : isSent
    ? `Last digest (${formatDate(u.window_ended_at)}) delivered`
    : `Last digest (${formatDate(u.window_ended_at)}) ${u.status}`
  return (
    <div className={`mb-4 flex items-start gap-2 px-3 py-2 rounded-md border text-sm ${tone}`}>
      <Mail size={14} className="mt-0.5 shrink-0" />
      <span>{label}</span>
    </div>
  )
}

const SEVERITY_ICONS = {
  critical: AlertOctagon,
  warning: AlertTriangle,
  info: Info,
}

const SEVERITY_ICON_COLORS = {
  critical: 'text-red-500',
  warning: 'text-amber-500',
  info: 'text-green-500',
}

const SEVERITY_STYLES = {
  critical: { bg: 'border-l-red-500', label: 'Critical' },
  warning: { bg: 'border-l-amber-500', label: 'Warning' },
  info: { bg: 'border-l-green-500', label: 'Info' },
}

const TABS = [
  { key: 'all', label: 'All' },
  { key: 'unread', label: 'Unread' },
  { key: 'critical', label: 'Critical' },
  { key: 'warning', label: 'Warning' },
  { key: 'info', label: 'Info' },
]

export default function Alerts() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [alerts, setAlerts] = useState([])
  const [rejectingAlertJobId, setRejectingAlertJobId] = useState(null)
  const [rejectReason, setRejectReason] = useState('')
  const [actionLoading, setActionLoading] = useState(null)
  const [loading, setLoading] = useState(true)
  const [activeTab, _setActiveTab] = useState(() => searchParams.get('filter') || 'all')
  const [hasMore, setHasMore] = useState(false)
  const [offset, setOffset] = useState(0)
  const LIMIT = 25

  const updateSearchParams = useCallback((updates) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      Object.entries(updates).forEach(([key, value]) => {
        if (value && value !== '' && !(key === 'filter' && value === 'all')) {
          next.set(key, value)
        } else {
          next.delete(key)
        }
      })
      return next
    }, { replace: true })
  }, [setSearchParams])

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
    } catch (err: any) {
      console.error('Failed to load alerts:', err)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadAlerts(true)
  }, [activeTab])

  const handleTabChange = useCallback((tab) => {
    _setActiveTab(tab)
    updateSearchParams({ filter: tab })
  }, [updateSearchParams])

  const handleMarkRead = async (alertId) => {
    try {
      await alertsApi.markRead(alertId)
      setAlerts(prev => prev.map(a => a.id === alertId ? { ...a, is_read: true } : a))
    } catch (err: any) {
      toast.error('Failed to mark alert as read')
    }
  }

  const handleDismiss = async (alertId) => {
    try {
      await alertsApi.dismiss(alertId)
      setAlerts(prev => prev.filter(a => a.id !== alertId))
    } catch (err: any) {
      toast.error('Failed to dismiss alert')
    }
  }

  // v1.8.8: one-click "false alarm — resume print" on a vision auto-
  // pause alert. Hits the printer resume endpoint AND marks the alert
  // read in one click. Surfaced when the alert_type is a vision
  // detection class and the alert references a printer.
  const handleResumeFromAlert = async (alert) => {
    if (!alert.printer_id) return
    setActionLoading(alert.id)
    try {
      await fetchAPI(`/printers/${alert.printer_id}/resume`, { method: 'POST' })
      try { await alertsApi.markRead(alert.id) } catch { /* read failure is non-blocking */ }
      setAlerts(prev => prev.map(a => a.id === alert.id ? { ...a, is_read: true } : a))
      toast.success('Print resumed')
    } catch (err: any) {
      toast.error('Resume failed: ' + (err?.message || 'Unknown error'))
    } finally {
      setActionLoading(null)
    }
  }

  const VISION_ALERT_TYPES = new Set([
    'spaghetti_detected', 'first_layer_issue', 'detachment_detected',
  ])

  const handleMarkAllRead = async () => {
    try {
      await alertsApi.markAllRead()
      setAlerts(prev => prev.map(a => ({ ...a, is_read: true })))
    } catch (err: any) {
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
    } catch (err: any) {
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
    } catch (err: any) {
      console.error('Failed to reject job:', err)
    } finally {
      setActionLoading(null)
    }
  }

  const unreadCount = alerts.filter(a => !a.is_read).length

  return (
    <div className="p-4 md:p-6 max-w-4xl mx-auto">
      <PageHeader
        icon={Bell}
        title="Alerts"
        subtitle={unreadCount > 0 ? `${unreadCount} unread` : undefined}
      >
        {unreadCount > 0 && (
          <Button size="sm" onClick={handleMarkAllRead}>
            Mark all read
          </Button>
        )}
      </PageHeader>

      <DigestStatusStrip />

      <div className="flex gap-1 mb-6 p-1 rounded-md" style={{ backgroundColor: 'var(--brand-sidebar-bg)' }}>
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
          <EmptyState
            icon={Bell}
            title="No alerts to show"
          />
        ) : (
          alerts.map(alert => {
            const sev = SEVERITY_STYLES[alert.severity] || SEVERITY_STYLES.info
            const action = getActionLink(alert)

            return (
              <div
                key={alert.id}
                className={`rounded-md border border-l-4 ${sev.bg} transition-all ${!alert.is_read ? 'ring-1 ring-[var(--brand-primary)]/20' : ''}`}
                style={{
                  backgroundColor: 'var(--brand-card-bg)',
                  borderColor: 'var(--brand-sidebar-border)',
                  opacity: alert.is_read ? 0.7 : 1,
                }}
              >
                <div className="p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex gap-3 min-w-0 flex-1">
                      {(() => { const SevIcon = SEVERITY_ICONS[alert.severity] || Info; return <SevIcon size={18} className={`flex-shrink-0 ${SEVERITY_ICON_COLORS[alert.severity] || 'text-green-500'}`} /> })()}
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
                            <span className="text-[10px] px-1.5 py-0.5 rounded-md" style={{
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
                            className="p-1.5 rounded-md transition-colors hover:bg-green-900/50 text-green-400"
                            title="Approve Job"
                          >
                            <CheckCircle size={14} />
                          </button>
                          <button
                            onClick={() => setRejectingAlertJobId(rejectingAlertJobId === alert.id ? null : alert.id)}
                            disabled={actionLoading === alert.id}
                            className="p-1.5 rounded-md transition-colors hover:bg-red-900/50 text-red-400"
                            title="Reject Job"
                          >
                            <XCircle size={14} />
                          </button>
                        </>
                      )}
                      {action && alert.alert_type !== 'job_submitted' && (
                        <a
                          href={action.path}
                          className="p-1.5 rounded-md transition-colors hover:bg-[var(--brand-input-bg)] text-[var(--brand-text-muted)]"
                          title={action.label}
                        >
                          <ExternalLink size={14} />
                        </a>
                      )}
                      {/* v1.8.8: one-click "false alarm — resume" on
                          vision auto-pause alerts. */}
                      {VISION_ALERT_TYPES.has(alert.alert_type) && !alert.is_read && alert.printer_id && (
                        <button
                          onClick={() => handleResumeFromAlert(alert)}
                          disabled={actionLoading === alert.id}
                          className="p-1.5 rounded-md transition-colors hover:bg-green-900/40 text-green-400"
                          title="False alarm — resume print"
                        >
                          <CheckCircle size={14} />
                        </button>
                      )}
                      {!alert.is_read && (
                        <button
                          onClick={() => handleMarkRead(alert.id)}
                          className="p-1.5 rounded-md transition-colors hover:bg-[var(--brand-input-bg)] text-[var(--brand-text-muted)]"
                          title="Mark read"
                        >
                          <Check size={14} />
                        </button>
                      )}
                      <button
                        onClick={() => handleDismiss(alert.id)}
                        className="p-1.5 rounded-md transition-colors hover:bg-red-900/50 text-[var(--brand-text-muted)] hover:text-red-400"
                        title="Dismiss"
                      >
                        <X size={14} />
                      </button>
                    </div>
                  </div>
                </div>
                  {rejectingAlertJobId === alert.id && (
                    <div className="mt-3 pt-3 px-4 pb-4 border-t border-[var(--brand-card-border)]">
                      <label className="block text-xs text-[var(--brand-text-muted)] mb-1">Rejection reason (required):</label>
                      <div className="flex gap-2">
                        <input
                          type="text"
                          value={rejectReason}
                          onChange={(e) => setRejectReason(e.target.value)}
                          placeholder="e.g., Too much filament — please re-slice with 10% infill"
                          className="flex-1 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-2 py-1.5 text-sm text-white"
                          autoFocus
                          onKeyDown={(e) => e.key === 'Enter' && rejectReason.trim() && handleRejectFromAlert(alert)}
                        />
                        <Button
                          variant="danger"
                          size="sm"
                          onClick={() => handleRejectFromAlert(alert)}
                          disabled={!rejectReason.trim() || actionLoading === alert.id}
                        >
                          Reject
                        </Button>
                        <Button
                          variant="tertiary"
                          size="sm"
                          onClick={() => { setRejectingAlertJobId(null); setRejectReason('') }}
                        >
                          Cancel
                        </Button>
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
          <Button
            variant="secondary"
            onClick={() => loadAlerts(false)}
            loading={loading}
          >
            Load more...
          </Button>
        </div>
      )}
    </div>
  )
}
