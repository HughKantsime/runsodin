import { useId, useState, type ReactNode } from 'react'
import { Check, ChevronDown, X } from 'lucide-react'
import CompletionSummary from '../../components/jobs/CompletionSummary'
import { formatDate } from '../../utils/shared'
import type { Alert } from '../../types/notification'
import type { AlertGroup } from './alertGroups'

interface Props {
  group: AlertGroup<Alert>
  icon: ReactNode
  borderClass: string
  busy: boolean
  onMarkRead: (ids: number[]) => void
  onDismiss: (ids: number[]) => void
  /** Existing per-alert action buttons, reused for each member. */
  renderActions: (alert: Alert) => ReactNode
  /** Existing per-alert extras (reject form), reused for each member. */
  renderExtra: (alert: Alert) => ReactNode
}

export default function AlertGroupRow({ group, icon, borderClass, busy, onMarkRead, onDismiss, renderActions, renderExtra }: Props) {
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const { newest, oldest, alerts, anyUnread } = group
  const count = alerts.length
  const unreadIds = alerts.filter(a => !a.is_read).map(a => a.id)
  const liveIds = alerts.filter(a => !a.is_dismissed).map(a => a.id)
  const summary = (newest.metadata_json as any)?.summary

  return (
    <div
      className={`rounded-md border border-l-4 ${borderClass} transition-all ${anyUnread ? 'ring-1 ring-[var(--brand-primary)]/20' : ''}`}
      style={{
        backgroundColor: 'var(--brand-card-bg)',
        borderColor: 'var(--brand-sidebar-border)',
        opacity: anyUnread ? 1 : 0.7,
      }}
    >
      <div className="p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="flex gap-3 min-w-0 flex-1">
            {icon}
            <div className="min-w-0 flex-1">
              <div className="flex items-start gap-2">
                <div
                  className={`text-sm font-semibold break-words min-w-0 ${open ? '' : 'line-clamp-2'}`}
                  style={{ color: 'var(--brand-text-primary)' }}
                  title={newest.title}
                >
                  {newest.title}
                </div>
                <span
                  className="shrink-0 text-[10px] font-semibold px-1.5 py-0.5 rounded-md"
                  style={{ backgroundColor: 'var(--brand-sidebar-bg)', color: 'var(--brand-text-secondary)' }}
                  title={`${count} alerts`}
                >
                  {`x${count}`}
                </span>
              </div>
              {summary && <CompletionSummary summary={summary} />}
              {newest.message && (
                <p className="text-xs mt-1 leading-relaxed" style={{ color: 'var(--brand-text-secondary)' }}>
                  {newest.message}
                </p>
              )}
              <div className="flex items-center gap-3 mt-2 flex-wrap">
                <span className="text-[10px]" style={{ color: 'var(--brand-text-muted)' }}>
                  {`first ${formatDate(oldest.created_at)}, last ${formatDate(newest.created_at)}`}
                </span>
                {newest.printer_name && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded-md" style={{
                    backgroundColor: 'var(--brand-sidebar-bg)',
                    color: 'var(--brand-text-muted)',
                  }}>
                    {newest.printer_name}
                  </span>
                )}
              </div>
            </div>
          </div>

          <div className="flex items-center gap-1 shrink-0">
            {unreadIds.length > 0 && (
              <button
                onClick={() => onMarkRead(unreadIds)}
                disabled={busy}
                aria-label={`Mark all ${count} alerts read`}
                title={`Mark all ${count} alerts read`}
                className="p-1.5 rounded-md transition-colors hover:bg-[var(--brand-input-bg)] text-[var(--brand-text-muted)] disabled:opacity-50"
              >
                <Check size={14} />
              </button>
            )}
            {liveIds.length > 0 && (
              <button
                onClick={() => onDismiss(liveIds)}
                disabled={busy}
                aria-label={`Dismiss all ${count} alerts`}
                title={`Dismiss all ${count} alerts`}
                className="p-1.5 rounded-md transition-colors hover:bg-red-900/50 text-[var(--brand-text-muted)] hover:text-red-400 disabled:opacity-50"
              >
                <X size={14} />
              </button>
            )}
            <button
              onClick={() => setOpen(o => !o)}
              aria-expanded={open}
              aria-controls={panelId}
              aria-label={open ? `Hide ${count} alerts` : `Show ${count} alerts`}
              title={open ? 'Hide alerts' : 'Show alerts'}
              className="p-1.5 rounded-md transition-colors hover:bg-[var(--brand-input-bg)] text-[var(--brand-text-muted)]"
            >
              <ChevronDown size={14} className={open ? 'rotate-180 transition-transform' : 'transition-transform'} />
            </button>
          </div>
        </div>
      </div>
      {open && (
        <ul id={panelId} className="border-t divide-y" style={{ borderColor: 'var(--brand-sidebar-border)' }}>
          {alerts.map(a => (
            <li key={a.id}>
              <div className="flex items-center justify-between gap-3 px-4 py-2" style={{ opacity: a.is_read ? 0.7 : 1 }}>
                <div className="min-w-0 flex-1">
                  <span className="text-[10px]" style={{ color: 'var(--brand-text-muted)' }}>
                    {formatDate(a.created_at)}
                  </span>
                  {a.message && (
                    <p className="text-xs leading-relaxed" style={{ color: 'var(--brand-text-secondary)' }}>
                      {a.message}
                    </p>
                  )}
                </div>
                {renderActions(a)}
              </div>
              {renderExtra(a)}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
