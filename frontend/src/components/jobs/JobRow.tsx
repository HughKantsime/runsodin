import { Play, CheckCircle, XCircle, RotateCcw, Trash2, AlertTriangle, Calendar, Pencil, GripVertical, ShoppingCart, MessageSquare, RefreshCw, Send, Loader2 } from 'lucide-react'
import { format } from 'date-fns'
import clsx from 'clsx'
import { resolveColor } from '../../utils/colorMap'
import { canDo } from '../../permissions'
import { formatHours } from '../../utils/shared'
import { Button, StatusBadge } from '../ui'

function DueDateBadge({ dueDate }: { dueDate: string | null }) {
  if (!dueDate) return null
  const due = new Date(dueDate)
  const now = new Date()
  const daysUntil = Math.ceil((due - now) / (1000 * 60 * 60 * 24))

  let color = 'text-[var(--brand-text-secondary)]'
  if (daysUntil < 0) color = 'text-red-400'
  else if (daysUntil <= 1) color = 'text-orange-400'
  else if (daysUntil <= 3) color = 'text-yellow-400'

  const label = daysUntil < 0
    ? `${Math.abs(daysUntil)}d overdue`
    : daysUntil === 0 ? 'Due today'
    : daysUntil === 1 ? 'Due tomorrow'
    : `Due in ${daysUntil}d`

  return (
    <span className={`inline-flex items-center gap-1 text-xs ${color}`}>
      <Calendar size={10} />
      {label}
    </span>
  )
}

export default function JobRow({ job, onAction, dragProps, isSelected, onToggleSelect, isDispatching }: { job: any; onAction: (...args: any[]) => void; dragProps?: any; isSelected: boolean; onToggleSelect: (id: number) => void; isDispatching?: boolean }) {
  return (
    <tr className={clsx(
          'border-b border-[var(--brand-card-border)] hover:bg-[var(--brand-input-bg)]/50 even:bg-[var(--brand-content-bg)]/40',
          job.due_date && new Date(job.due_date) < new Date() && !['completed','cancelled','failed'].includes(job.status) && 'bg-red-950/30 border-l-2 border-l-red-500'
        )}
        {...(dragProps || {})}>
      <td className="px-2 py-3 w-10">
        <div className="flex items-center gap-1">
          {dragProps && (
            <GripVertical size={14} className="text-[var(--brand-text-muted)] flex-shrink-0 cursor-grab" title="Drag to reorder" />
          )}
          <input type="checkbox" checked={isSelected} onChange={() => onToggleSelect(job.id)} className="rounded border-[var(--brand-text-muted)]" aria-label={`Select job ${job.item_name}`} />
        </div>
      </td>
      <td className="px-3 md:px-4 py-3">
        <StatusBadge status={job.status} />
      </td>
      <td className="px-3 md:px-4 py-3">
        <div className="font-medium text-sm">{job.item_name}</div>
        {job.order_item_id && (
          <div className="text-xs text-[var(--brand-primary)] flex items-center gap-1">
            <ShoppingCart size={10} />
            Order #{job.order_item?.order_id || '—'}
          </div>
        )}
        {job.rejected_reason && (
          <div className="text-xs text-red-400 flex items-center gap-1 truncate max-w-xs">
            <MessageSquare size={10} />
            {job.rejected_reason}
          </div>
        )}
        {job.notes && <div className="text-xs text-[var(--brand-text-muted)] truncate max-w-xs">{job.notes}</div>}
        {job.due_date && <DueDateBadge dueDate={job.due_date} />}
        {job.fail_reason && (
          <div className="text-xs text-red-400 truncate max-w-xs">
            <AlertTriangle size={10} className="flex-shrink-0" /> {job.fail_reason.replace(/_/g, ' ')}{job.fail_notes ? `: ${job.fail_notes}` : ''}
          </div>
        )}
      </td>
      <td className="px-3 md:px-4 py-3">
        <span className={clsx(
          'px-2 py-0.5 rounded-md text-xs font-medium',
          job.priority <= 2 ? 'bg-red-900/50 text-red-400' :
          job.priority >= 4 ? 'bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)]' :
          'bg-amber-900/50 text-amber-400'
        )}>
          P{job.priority}
        </span>
      </td>
      <td className="px-3 md:px-4 py-3 text-sm">{job.printer?.name || '—'}</td>
      <td className="px-3 md:px-4 py-3 hidden lg:table-cell">
        {job.colors_list?.length > 0 ? (
          <div className="flex gap-1 flex-wrap">
            {job.colors_list.map((color, i) => (
              <span key={i} className="w-5 h-5 rounded-full border border-[var(--brand-card-border)] flex items-center justify-center" style={{ backgroundColor: resolveColor(color) || "#333" }} title={color}>{!resolveColor(color) && <span className="text-[10px] text-[var(--brand-text-secondary)]">?</span>}</span>
            ))}
          </div>
        ) : '—'}
      </td>
      <td className="px-3 md:px-4 py-3 text-sm text-[var(--brand-text-secondary)] hidden md:table-cell">
        <span className="font-mono">{formatHours(job.duration_hours)}</span>
        {job.actual_start && job.actual_end && (() => {
          const actualH = (new Date(job.actual_end) - new Date(job.actual_start)) / 3600000
          return (
            <span className="block text-xs text-[var(--brand-text-muted)] font-mono" title="Actual duration">
              {formatHours(actualH)} actual
            </span>
          )
        })()}
      </td>
      <td className="px-3 md:px-4 py-3 text-sm text-[var(--brand-text-secondary)] hidden lg:table-cell">
        <span className="font-mono">{job.scheduled_start ? format(new Date(job.scheduled_start), 'MMM d HH:mm') : '—'}</span>
      </td>
      <td className="px-3 md:px-4 py-3">
        <div className="flex items-center gap-1">
          {job.status === 'submitted' && canDo('jobs.approve') && (
            <>
              <Button variant="ghost" size="icon" icon={CheckCircle} onClick={() => onAction('approve', job.id)} className="text-green-400 hover:bg-green-900/50" aria-label="Approve job" />
              <Button variant="ghost" size="icon" icon={XCircle} onClick={() => onAction('reject', job.id)} className="text-red-400 hover:bg-red-900/50" aria-label="Reject job" />
            </>
          )}
          {job.status === 'rejected' && job.submitted_by && canDo('jobs.resubmit') && (
            <Button variant="ghost" size="icon" icon={RefreshCw} onClick={() => onAction('resubmit', job.id)} className="text-amber-400 hover:bg-amber-900/50" aria-label="Resubmit job" />
          )}
          {job.status === 'scheduled' && canDo('jobs.start') && (
            <Button variant="ghost" size="icon" icon={Play} onClick={() => onAction('start', job.id)} className="text-[var(--brand-primary)] hover:bg-[var(--brand-primary)]/10" aria-label="Start print (manual)" />
          )}
          {job.status === 'scheduled' && job.printer_id && canDo('jobs.start') && (
            <Button
              variant="ghost"
              size="icon"
              icon={isDispatching ? Loader2 : Send}
              loading={isDispatching}
              onClick={() => !isDispatching && onAction('dispatch', job.id, job.item_name, job.printer?.name)}
              className={clsx(isDispatching ? 'text-emerald-500' : 'text-emerald-400 hover:bg-emerald-900/50')}
              title={isDispatching ? 'Dispatching...' : `Dispatch to ${job.printer?.name || 'printer'} — uploads file via FTP and starts print`}
              aria-label={isDispatching ? 'Dispatching...' : 'Dispatch to printer'}
              disabled={isDispatching}
            />
          )}
          {job.status === 'printing' && canDo('jobs.complete') && (
            <>
              <Button variant="ghost" size="icon" icon={CheckCircle} onClick={() => onAction('complete', job.id)} className="text-green-400 hover:bg-green-900/50" aria-label="Mark job complete" />
              <Button variant="ghost" size="icon" icon={AlertTriangle} onClick={() => onAction('markFailed', job.id, job.item_name)} className="text-red-400 hover:bg-red-900/50" aria-label="Mark job failed" />
            </>
          )}
          {canDo('jobs.edit') && ['pending', 'scheduled', 'submitted'].includes(job.status) && (
            <Button variant="ghost" size="icon" icon={Pencil} onClick={() => onAction('edit', job.id)} className="text-[var(--brand-text-secondary)] hover:text-[var(--brand-primary)] hover:bg-[var(--brand-primary)]/10" aria-label="Edit job" />
          )}
          {canDo('jobs.cancel') && (job.status === 'scheduled' || job.status === 'printing') && (
            <Button variant="ghost" size="icon" icon={XCircle} onClick={() => onAction('cancel', job.id)} className="text-red-400 hover:bg-red-900/50" aria-label="Cancel job" />
          )}
          {job.status === 'failed' && (
            <Button variant="ghost" size="icon" icon={AlertTriangle} onClick={() => onAction('failReason', job.id, job.item_name, job.fail_reason, job.fail_notes)} className="text-amber-400 hover:bg-amber-900/50" aria-label={job.fail_reason ? 'Edit failure reason' : 'Add failure reason'} />
          )}
          {canDo('jobs.delete') && (job.status === 'pending' || job.status === 'scheduled' || job.status === 'failed') && (
            <>
              <Button variant="ghost" size="icon" icon={RefreshCw} onClick={() => onAction('repeat', job.id)} className="text-[var(--brand-text-secondary)] hover:text-[var(--brand-primary)] hover:bg-[var(--brand-primary)]/10" aria-label="Print again" />
              <Button variant="ghost" size="icon" icon={Trash2} onClick={() => onAction('delete', job.id)} className="text-[var(--brand-text-muted)] hover:text-red-400 hover:bg-red-900/50" aria-label="Delete job" />
            </>
          )}
        </div>
      </td>
    </tr>
  )
}
