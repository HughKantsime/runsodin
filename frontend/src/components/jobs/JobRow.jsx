import { Play, CheckCircle, XCircle, RotateCcw, Trash2, AlertTriangle, Calendar, Pencil, GripVertical, ShoppingCart, MessageSquare, RefreshCw, Send, Loader2 } from 'lucide-react'
import { format } from 'date-fns'
import clsx from 'clsx'
import { resolveColor } from '../../utils/colorMap'
import { canDo } from '../../permissions'
import { formatHours } from './jobUtils'

function DueDateBadge({ dueDate }) {
  if (!dueDate) return null
  const due = new Date(dueDate)
  const now = new Date()
  const daysUntil = Math.ceil((due - now) / (1000 * 60 * 60 * 24))

  let color = 'text-farm-400'
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

export default function JobRow({ job, onAction, dragProps, isSelected, onToggleSelect, isDispatching }) {
  const statusColors = {
    submitted: 'text-amber-400',
    pending: 'text-status-pending',
    scheduled: 'text-status-scheduled',
    printing: 'text-status-printing',
    completed: 'text-status-completed',
    failed: 'text-status-failed',
    rejected: 'text-red-400',
  }

  return (
    <tr className={clsx(
          'border-b border-farm-800 hover:bg-farm-800/50 even:bg-farm-950/40',
          job.due_date && new Date(job.due_date) < new Date() && !['completed','cancelled','failed'].includes(job.status) && 'bg-red-950/30 border-l-2 border-l-red-500'
        )}
        {...(dragProps || {})}>
      <td className="px-2 py-3 w-10">
        <div className="flex items-center gap-1">
          {dragProps && (
            <GripVertical size={14} className="text-farm-600 flex-shrink-0 cursor-grab" title="Drag to reorder" />
          )}
          <input type="checkbox" checked={isSelected} onChange={() => onToggleSelect(job.id)} className="rounded border-farm-600" aria-label={`Select job ${job.item_name}`} />
        </div>
      </td>
      <td className="px-3 md:px-4 py-3">
        <div className="flex items-center gap-2">
          <div className={clsx('status-dot', job.status)} />
          <span className={clsx('text-sm', statusColors[job.status])}>{job.status}</span>
        </div>
      </td>
      <td className="px-3 md:px-4 py-3">
        <div className="font-medium text-sm">{job.item_name}</div>
        {job.order_item_id && (
          <div className="text-xs text-print-400 flex items-center gap-1">
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
        {job.notes && <div className="text-xs text-farm-500 truncate max-w-xs">{job.notes}</div>}
        {job.due_date && <DueDateBadge dueDate={job.due_date} />}
        {job.fail_reason && (
          <div className="text-xs text-red-400 truncate max-w-xs">
            ⚠ {job.fail_reason.replace(/_/g, ' ')}{job.fail_notes ? `: ${job.fail_notes}` : ''}
          </div>
        )}
      </td>
      <td className="px-3 md:px-4 py-3">
        <span className={clsx(
          'px-2 py-0.5 rounded-lg text-xs font-medium',
          job.priority <= 2 ? 'bg-red-900/50 text-red-400' :
          job.priority >= 4 ? 'bg-farm-800 text-farm-400' :
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
              <span key={i} className="w-5 h-5 rounded-full border border-farm-700 flex items-center justify-center" style={{ backgroundColor: resolveColor(color) || "#333" }} title={color}>{!resolveColor(color) && <span className="text-[10px] text-farm-400">?</span>}</span>
            ))}
          </div>
        ) : '—'}
      </td>
      <td className="px-3 md:px-4 py-3 text-sm text-farm-400 hidden md:table-cell">
        <span>{formatHours(job.duration_hours)}</span>
        {job.actual_start && job.actual_end && (() => {
          const actualH = (new Date(job.actual_end) - new Date(job.actual_start)) / 3600000
          return (
            <span className="block text-xs text-farm-500" title="Actual duration">
              {formatHours(actualH)} actual
            </span>
          )
        })()}
      </td>
      <td className="px-3 md:px-4 py-3 text-sm text-farm-400 hidden lg:table-cell">
        {job.scheduled_start ? format(new Date(job.scheduled_start), 'MMM d HH:mm') : '—'}
      </td>
      <td className="px-3 md:px-4 py-3">
        <div className="flex items-center gap-1">
          {job.status === 'submitted' && canDo('jobs.approve') && (
            <>
              <button onClick={() => onAction('approve', job.id)} className="p-1.5 text-green-400 hover:bg-green-900/50 rounded-lg" aria-label="Approve job">
                <CheckCircle size={16} />
              </button>
              <button onClick={() => onAction('reject', job.id)} className="p-1.5 text-red-400 hover:bg-red-900/50 rounded-lg" aria-label="Reject job">
                <XCircle size={16} />
              </button>
            </>
          )}
          {job.status === 'rejected' && job.submitted_by && canDo('jobs.resubmit') && (
            <button onClick={() => onAction('resubmit', job.id)} className="p-1.5 text-amber-400 hover:bg-amber-900/50 rounded-lg" aria-label="Resubmit job">
              <RefreshCw size={14} />
            </button>
          )}
          {job.status === 'scheduled' && canDo('jobs.start') && (
            <button onClick={() => onAction('start', job.id)} className="p-1.5 text-print-400 hover:bg-print-900/50 rounded-lg" aria-label="Start print (manual)">
              <Play size={16} />
            </button>
          )}
          {job.status === 'scheduled' && job.printer_id && canDo('jobs.start') && (
            <button
              onClick={() => !isDispatching && onAction('dispatch', job.id, job.item_name, job.printer?.name)}
              className={clsx(
                'p-1.5 rounded-lg',
                isDispatching
                  ? 'text-emerald-500 cursor-not-allowed'
                  : 'text-emerald-400 hover:bg-emerald-900/50'
              )}
              title={isDispatching ? 'Dispatching...' : `Dispatch to ${job.printer?.name || 'printer'} — uploads file via FTP and starts print`}
              aria-label={isDispatching ? 'Dispatching...' : 'Dispatch to printer'}
              disabled={isDispatching}
            >
              {isDispatching ? <Loader2 size={15} className="animate-spin" /> : <Send size={15} />}
            </button>
          )}
          {job.status === 'printing' && canDo('jobs.complete') && (
            <>
              <button onClick={() => onAction('complete', job.id)} className="p-1.5 text-green-400 hover:bg-green-900/50 rounded-lg" aria-label="Mark job complete">
                <CheckCircle size={16} />
              </button>
              <button onClick={() => onAction('markFailed', job.id, job.item_name)} className="p-1.5 text-red-400 hover:bg-red-900/50 rounded-lg" aria-label="Mark job failed">
                <AlertTriangle size={16} />
              </button>
            </>
          )}
          {canDo('jobs.edit') && ['pending', 'scheduled', 'submitted'].includes(job.status) && (
            <button onClick={() => onAction('edit', job.id)} className="p-1.5 text-farm-400 hover:text-print-400 hover:bg-print-900/50 rounded-lg" aria-label="Edit job">
              <Pencil size={14} />
            </button>
          )}
          {canDo('jobs.cancel') && (job.status === 'scheduled' || job.status === 'printing') && (
            <button onClick={() => onAction('cancel', job.id)} className="p-1.5 text-red-400 hover:bg-red-900/50 rounded-lg" aria-label="Cancel job">
              <XCircle size={16} />
            </button>
          )}
          {job.status === 'failed' && (
            <button onClick={() => onAction('failReason', job.id, job.item_name, job.fail_reason, job.fail_notes)} className="p-1.5 text-amber-400 hover:bg-amber-900/50 rounded-lg" aria-label={job.fail_reason ? 'Edit failure reason' : 'Add failure reason'}>
              <AlertTriangle size={16} />
            </button>
          )}
          {canDo('jobs.delete') && (job.status === 'pending' || job.status === 'scheduled' || job.status === 'failed') && (
            <>
              <button onClick={() => onAction('repeat', job.id)} className="p-1.5 text-farm-400 hover:text-print-400 hover:bg-print-900/50 rounded-lg" aria-label="Print again">
                <RefreshCw size={14} />
              </button>
              <button onClick={() => onAction('delete', job.id)} className="p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded-lg" aria-label="Delete job">
                <Trash2 size={16} />
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  )
}
