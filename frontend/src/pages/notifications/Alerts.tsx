import { useFoundry } from '../../hooks/themePreferences'
import PageState from '../../components/ui/PageState'
import CompletionSummary from '../../components/jobs/CompletionSummary'
import { useState, useEffect, useCallback, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Bell, Check, X, Trash2, ExternalLink, CheckCircle, XCircle, AlertOctagon, AlertTriangle, Info, Mail } from 'lucide-react'
import toast from 'react-hot-toast'
import { alerts as alertsApi, approveJob, rejectJob, fetchAPI, printers as printersApi } from '../../api'
import { getCurrentUser } from '../../permissions'
import { formatDate } from '../../utils/shared'
import { PageHeader, Button, EmptyState } from '../../components/ui'
import ScrollTabs from '../../components/ui/ScrollTabs'
import type { AlertListParams } from '../../types/notification'
import AlertGroupRow from './AlertGroupRow'
import AlertTitle from './AlertTitle'
import { groupAlerts } from './alertGroups'

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
  { key: 'dismissed', label: 'Dismissed' },
]

export default function Alerts() {
  const foundry = useFoundry()
  const [searchParams, setSearchParams] = useSearchParams()
  const [alerts, setAlerts] = useState([])
  const [deletingIds, setDeletingIds] = useState<Set<number>>(new Set())
  const listGeneration = useRef(0)
  // What is on screen now, for callbacks that finish after the list or its filter changed.
  const shownAlerts = useRef<any[]>([])
  shownAlerts.current = alerts
  const deletedIds = useRef(new Set<number>())
  const pendingDeletes = useRef(new Set<number>())
  const canDelete = ['operator', 'admin'].includes(getCurrentUser()?.role ?? '')
  const [rejectingAlertJobId, setRejectingAlertJobId] = useState(null)
  const [rejectReason, setRejectReason] = useState('')
  const [actionLoading, setActionLoading] = useState(null)
  const [loading, setLoading] = useState(true)
  const [activeTab, _setActiveTab] = useState(() => searchParams.get('filter') || 'all')
  const [hasMore, setHasMore] = useState(false)
  const [offset, setOffset] = useState(0)
  const LIMIT = 25

  const updateSearchParams = useCallback((updates: Record<string, string>) => {
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
    const generation = ++listGeneration.current
    try {
      setLoading(true)
      const newOffset = reset ? 0 : offset
      const params: AlertListParams = { limit: LIMIT, offset: newOffset }

      // The server hides dismissed alerts by default; the Dismissed tab lists only them.
      if (activeTab === 'unread') {
        params.is_read = false
      } else if (activeTab === 'dismissed') {
        params.is_dismissed = true
      } else if (['critical', 'warning', 'info'].includes(activeTab)) {
        params.severity = activeTab as AlertListParams['severity']
      }

      const response = await alertsApi.list(params)
      if (generation !== listGeneration.current) return
      const data = response.filter(alert => !deletedIds.current.has(alert.id))
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
      if (generation === listGeneration.current) setLoading(false)
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

  const handleDelete = async (alertId: number) => {
    if (!canDelete || pendingDeletes.current.has(alertId)) return
    pendingDeletes.current.add(alertId)
    setDeletingIds(new Set(pendingDeletes.current))
    try {
      const receipt = await alertsApi.remove(alertId)
      if (receipt?.status !== 'ok' || receipt.alert_id !== alertId) throw new Error('Deletion was not confirmed')
      deletedIds.current.add(alertId)
      setAlerts(prev => prev.filter(alert => alert.id !== alertId))
      setOffset(prev => Math.max(0, prev - 1))
      window.dispatchEvent(new Event('odin:alerts-changed'))
    } catch {
      toast.error('Failed to delete alert')
    } finally {
      pendingDeletes.current.delete(alertId)
      setDeletingIds(new Set(pendingDeletes.current))
    }
  }

  const handleDismiss = async (alertId) => {
    if (groupBusyRef.current) return   // a group action is dismissing these: never count one twice
    try {
      await alertsApi.dismiss(alertId)
      setAlerts(prev => prev.filter(a => a.id !== alertId))
      // The server no longer lists it: keep the next page's offset aligned.
      setOffset(prev => Math.max(0, prev - 1))
      window.dispatchEvent(new Event('odin:alerts-changed'))
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

  // Group-level actions: sequential, per-alert API calls. Only members whose call
  // succeeded change state, so failed members stay visible; a partial failure is
  // reported with the page's toast pattern.
  const [groupBusy, setGroupBusy] = useState(false)
  const groupBusyRef = useRef(false)
  const runGroupAction = async (
    ids: number[],
    call: (id: number) => Promise<unknown>,
    verb: string,
    onDone: (done: number[]) => void,
  ) => {
    if (groupBusyRef.current) return
    groupBusyRef.current = true
    setGroupBusy(true)
    try {
      const done: number[] = []
      for (const id of ids) {
        try {
          await call(id)
          done.push(id)
        } catch { /* counted below */ }
      }
      if (done.length > 0) onDone(done)
      if (done.length < ids.length) {
        toast.error(`Failed to ${verb} ${ids.length - done.length} of ${ids.length} alerts`)
      }
    } finally {
      groupBusyRef.current = false
      setGroupBusy(false)
    }
  }

  const handleGroupMarkRead = (ids: number[]) =>
    runGroupAction(ids, id => alertsApi.markRead(id), 'mark read', done => {
      const set = new Set(done)
      setAlerts(prev => prev.map(a => set.has(a.id) ? { ...a, is_read: true } : a))
    })

  const handleGroupDismiss = (ids: number[]) =>
    runGroupAction(ids, id => alertsApi.dismiss(id), 'dismiss', done => {
      const set = new Set(done)
      // Only alerts still in the list on screen move the next page's offset: after a switch
      // to another filter the dismissed ones were never part of that list.
      const removed = shownAlerts.current.filter(a => set.has(a.id)).length
      setAlerts(prev => prev.filter(a => !set.has(a.id)))
      setOffset(prev => Math.max(0, prev - removed))
      window.dispatchEvent(new Event('odin:alerts-changed'))
    })

  // Per-alert action buttons, shared by single rows and group members.
  const renderActions = (alert) => (
    <div className="flex items-center gap-1 shrink-0">
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
      {getActionLink(alert) && alert.alert_type !== 'job_submitted' && (
        <a
          href={getActionLink(alert).path}
          className="p-1.5 rounded-md transition-colors hover:bg-[var(--brand-input-bg)] text-[var(--brand-text-muted)]"
          title={getActionLink(alert).label}
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
      {canDelete && (
        <button
          onClick={() => handleDelete(alert.id)}
          disabled={deletingIds.has(alert.id)}
          aria-label="Delete alert"
          title="Delete alert"
          className="p-1.5 rounded-md transition-colors hover:bg-red-900/50 text-[var(--brand-text-muted)] hover:text-red-400 disabled:opacity-50"
        >
          <Trash2 size={14} />
        </button>
      )}
      {!alert.is_dismissed && (
        <button
          onClick={() => handleDismiss(alert.id)}
          className="p-1.5 rounded-md transition-colors hover:bg-red-900/50 text-[var(--brand-text-muted)] hover:text-red-400"
          title="Dismiss"
        >
          <X size={14} />
        </button>
      )}
    </div>
  )

  const renderRejectForm = (alert) => rejectingAlertJobId === alert.id && (
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
  )

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

      <ScrollTabs className="flex gap-1 mb-6 p-1 rounded-md max-sm:overflow-x-auto" style={{ backgroundColor: 'var(--brand-sidebar-bg)' }} fadeClassName="from-[var(--brand-sidebar-bg)]">
        {TABS.map(tab => (
          <button
            key={tab.key}
            onClick={() => handleTabChange(tab.key)}
            data-scrolltab-active={activeTab === tab.key ? 'true' : undefined}
            className="px-3 py-1.5 text-xs font-medium rounded-md transition-colors"
            style={activeTab === tab.key
              ? { backgroundColor: 'var(--brand-accent)', color: foundry ? 'var(--brand-on-primary)' : 'white' }
              : { color: 'var(--brand-text-muted)' }
            }
          >
            {tab.label}
          </button>
        ))}
      </ScrollTabs>

      <div className="space-y-3">
        {loading && alerts.length === 0 ? (
          <PageState kind="loading" className="text-center py-12" style={{ color: 'var(--brand-text-muted)' }}>Loading alerts...</PageState>
        ) : alerts.length === 0 ? (
          <EmptyState
            icon={Bell}
            title="No alerts to show"
          />
        ) : (
          // Grouping runs on the rows loaded so far only (this page paginates with
          // "Load more"); repeats that sit on a page not yet loaded are not merged.
          groupAlerts(alerts).map(group => {
            if (group.alerts.length > 1) {
              const groupSev = SEVERITY_STYLES[group.newest.severity] || SEVERITY_STYLES.info
              const GroupIcon = SEVERITY_ICONS[group.newest.severity] || Info
              return (
                <AlertGroupRow
                  key={group.newest.id}
                  group={group}
                  icon={<GroupIcon size={18} className={`shrink-0 ${SEVERITY_ICON_COLORS[group.newest.severity] || 'text-green-500'}`} />}
                  borderClass={groupSev.bg}
                  busy={groupBusy}
                  onMarkRead={handleGroupMarkRead}
                  onDismiss={handleGroupDismiss}
                  renderActions={renderActions}
                  renderExtra={renderRejectForm}
                />
              )
            }
            const alert = group.alerts[0]
            const sev = SEVERITY_STYLES[alert.severity] || SEVERITY_STYLES.info

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
                      {(() => { const SevIcon = SEVERITY_ICONS[alert.severity] || Info; return <SevIcon size={18} className={`shrink-0 ${SEVERITY_ICON_COLORS[alert.severity] || 'text-green-500'}`} /> })()}
                      <div className="min-w-0 flex-1">
                        <AlertTitle title={alert.title} />
                        {alert.metadata_json?.summary && <CompletionSummary summary={alert.metadata_json.summary} />}
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

                    {renderActions(alert)}
                  </div>
                </div>
                  {renderRejectForm(alert)}
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
