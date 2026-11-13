import { useState, type ReactNode } from 'react'
import OutcomeCorrection from './OutcomeCorrection'
import FilamentCheckPanel from './FilamentCheckPanel'
import CheckTarget from '../ui/CheckTarget'
import { displayName } from '../../utils/displayName'
import PageState from '../ui/PageState'
import { Play, CheckCircle, XCircle, RotateCcw, Trash2, AlertTriangle, Calendar, Pencil, GripVertical, ShoppingCart, MessageSquare, RefreshCw, Send, Loader2, ChevronUp, ChevronDown } from 'lucide-react'
import { format } from 'date-fns'
import clsx from 'clsx'
import { resolveColor } from '../../utils/colorMap'
import { canDo, getCurrentUser } from '../../permissions'
import { formatHours } from '../../utils/shared'
import { Button, StatusBadge } from '../ui'

const PIN_ISSUE_TEXT: Record<string, string> = {
  pinned_printer_inactive: 'Pinned printer is inactive — job stays queued',
  pinned_printer_missing: 'Pinned printer no longer exists — job stays queued',
  pinned_printer_deleted: 'Pinned printer was deleted — choose another printer',
  pinned_printer_inaccessible: 'Your organization can no longer use the pinned printer — choose another',
  pinned_target_invalid: 'Invalid printer pin — treated as any printer; edit the job to choose one',
}

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

// Shared by the table row and the phone card so both render the same content and rules.
function JobItemDetails({ job, nameClassName = 'font-medium text-sm' }: { job: any; nameClassName?: string }) {
  return (
    <>
      <div className={nameClassName}>{displayName(job)}</div>
      <div className="text-xs font-mono text-[var(--brand-text-muted)]">{`Job ${job.id}`}</div>
      {(job.quantity == null || job.hold == null || job.is_locked == null) && (
        <div className="text-xs text-[var(--brand-text-muted)]">
          {[job.quantity == null && 'Quantity unknown', job.hold == null && 'Hold state unknown', job.is_locked == null && 'Lock state unknown'].filter(Boolean).join(' · ')}
        </div>
      )}
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
          <AlertTriangle size={10} className="shrink-0" /> {job.fail_reason.replace(/_/g, ' ')}{job.fail_notes ? `: ${job.fail_notes}` : ''}
        </div>
      )}
    </>
  )
}

function PriorityBadge({ priority }: { priority: number }) {
  return (
    <span className={clsx(
      'px-2 py-0.5 rounded-md text-xs font-medium',
      priority <= 2 ? 'bg-red-900/50 text-red-400' :
      priority >= 4 ? 'bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)]' :
      'bg-amber-900/50 text-amber-400'
    )}>
      P{priority}
    </span>
  )
}

// The pre-dispatch filament check, offered on a scheduled job that has a printer (table row and phone card alike).
function FilamentToggle({ job, open, onToggle }: { job: any; open: boolean; onToggle: () => void }) {
  if (job.status !== 'scheduled' || job.printer_id == null) return null
  return (
    <button type="button" aria-expanded={open} onClick={onToggle}
      className="block min-h-[24px] text-xs underline text-[var(--brand-text-muted)]">
      {open ? 'Hide filament check' : 'Filament check'}
    </button>
  )
}

function JobPrinterInfo({ job, pinnedPrinterName, filament }: { job: any; pinnedPrinterName?: string; filament?: ReactNode }) {
  return (
    <>
      {job.printer?.name || '—'}
      {filament}
      {job.pinned_printer_id != null && (
        <div className="text-xs text-[var(--brand-text-muted)]" title="Only scheduled on this printer">
          Pinned: {pinnedPrinterName || `printer #${job.pinned_printer_id}`}
        </div>
      )}
      {job.pinned_printer_issue && (
        <div className="text-xs text-red-400" role="status">{PIN_ISSUE_TEXT[job.pinned_printer_issue] ?? 'Pinned printer is unavailable — job stays queued'}</div>
      )}
    </>
  )
}

function JobActions({ job, onAction, isDispatching, dispatchFenced, correctionPrincipal, className = 'flex items-center gap-1' }: { job: any; onAction: (...args: any[]) => void; isDispatching?: boolean; dispatchFenced?: boolean; correctionPrincipal?: any; className?: string }) {
  return (
    <div className={className}>
      {job.status === 'submitted' && canDo('jobs.approve') && (
        <>
          <Button variant="ghost" size="icon" icon={CheckCircle} onClick={() => onAction('approve', job.id)} className="text-green-400 hover:bg-green-900/50" aria-label="Approve job" />
          <Button variant="ghost" size="icon" icon={XCircle} onClick={() => onAction('reject', job.id)} className="text-red-400 hover:bg-red-900/50" aria-label="Reject job" />
        </>
      )}
      {job.status === 'rejected' && job.submitted_by != null && getCurrentUser()?.id === job.submitted_by && canDo('jobs.resubmit') && (
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
          onClick={() => !isDispatching && onAction('dispatch', job.id, displayName(job), job.printer?.name)}
          className={clsx(isDispatching ? 'text-emerald-500' : 'text-emerald-400 hover:bg-emerald-900/50')}
          title={dispatchFenced ? 'Dispatch requires verification. Check the printer before any further dispatch.' : isDispatching ? 'Dispatching...' : `Dispatch to ${job.printer?.name || 'printer'} — uploads file via FTP and starts print`}
          aria-label={dispatchFenced ? 'Dispatch requires verification' : isDispatching ? 'Dispatching...' : 'Dispatch to printer'}
          disabled={isDispatching || dispatchFenced}
        />
      )}
      {job.status === 'printing' && canDo('jobs.complete') && (
        <>
          <Button variant="ghost" size="icon" icon={CheckCircle} onClick={() => onAction('complete', job.id)} className="text-green-400 hover:bg-green-900/50" aria-label="Mark job complete" />
          <Button variant="ghost" size="icon" icon={AlertTriangle} onClick={() => onAction('markFailed', job.id, displayName(job))} className="text-red-400 hover:bg-red-900/50" aria-label="Mark job failed" />
        </>
      )}
      {(canDo('jobs.edit') && ['pending', 'scheduled', 'submitted'].includes(job.status) ||
        job.status === 'rejected' && correctionPrincipal?.role === 'viewer' && Number.isInteger(correctionPrincipal.id) && correctionPrincipal.id > 0 && correctionPrincipal.id === job.submitted_by && Number.isInteger(correctionPrincipal.group_id) && correctionPrincipal.group_id > 0 && correctionPrincipal.group_id === job.charged_to_org_id) && (
        <Button variant="ghost" size="icon" icon={Pencil} onClick={() => onAction('edit', job.id)} className="text-[var(--brand-text-secondary)] hover:text-[var(--brand-primary)] hover:bg-[var(--brand-primary)]/10" aria-label="Edit job" />
      )}
      {canDo('jobs.cancel') && ['pending', 'submitted', 'scheduled', 'printing'].includes(job.status) && (
        <Button variant="ghost" size="icon" icon={XCircle} onClick={() => onAction('cancel', job.id)} className="text-red-400 hover:bg-red-900/50" aria-label="Cancel job" />
      )}
      {job.status === 'failed' && (
        <Button variant="ghost" size="icon" icon={AlertTriangle} onClick={() => onAction('failReason', job.id, displayName(job), job.fail_reason, job.fail_notes)} className="text-amber-400 hover:bg-amber-900/50" aria-label={job.fail_reason ? 'Edit failure reason' : 'Add failure reason'} />
      )}
      {canDo('jobs.create') && ['pending', 'scheduled', 'completed', 'failed'].includes(job.status) && (
        <Button variant="ghost" size="icon" icon={RefreshCw} onClick={() => onAction('repeat', job.id)} className="text-[var(--brand-text-secondary)] hover:text-[var(--brand-primary)] hover:bg-[var(--brand-primary)]/10" aria-label="Print again" />
      )}
      {canDo('jobs.delete') && (job.status === 'pending' || job.status === 'scheduled' || job.status === 'failed') && (
        <Button variant="ghost" size="icon" icon={Trash2} onClick={() => onAction('delete', job.id)} className="text-[var(--brand-text-muted)] hover:text-red-400 hover:bg-red-900/50" aria-label="Delete job" />
      )}
    </div>
  )
}

// Phone layout (< 640 px): one stacked card per job. Order: name (wraps by words),
// status, printer, priority/time, then the same row actions.
function JobCard({ job, onAction, isSelected, onToggleSelect, isDispatching, dispatchFenced, correctionPrincipal, pinnedPrinterName, highlight, onMove }: { job: any; onAction: (...args: any[]) => void; isSelected: boolean; onToggleSelect: (id: number) => void; isDispatching?: boolean; dispatchFenced?: boolean; correctionPrincipal?: any; pinnedPrinterName?: string; highlight?: boolean; onMove?: (jobId: number, direction: -1 | 1) => void }) {
  const overdue = job.due_date && new Date(job.due_date) < new Date() && !['completed', 'cancelled', 'failed'].includes(job.status)
  const [showFilament, setShowFilament] = useState(false)
  return (
    <li data-testid="job-card" data-job-id={job.id} className={clsx(
      'px-3 py-3 border-b border-[var(--brand-card-border)] last:border-b-0',
      highlight && 'ring-2 ring-inset ring-[var(--brand-primary)]',
      overdue && 'bg-red-950/30 border-l-2 border-l-red-500'
    )}>
      <div className="flex items-start gap-2">
        <CheckTarget touch><input type="checkbox" checked={isSelected} onChange={() => onToggleSelect(job.id)} className="h-6 w-6 mt-0.5 shrink-0 rounded border-[var(--brand-text-muted)]" aria-label={`Select job ${displayName(job)}`} /></CheckTarget>
        <div className="min-w-0 flex-1">
          <JobItemDetails job={job} nameClassName="font-medium text-sm break-words" />
        </div>
        {job.queue_position != null && ['pending', 'scheduled'].includes(job.status) && (
          <span className="text-xs font-mono text-[var(--brand-text-muted)] shrink-0" title="Queue position">#{job.queue_position + 1}</span>
        )}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <StatusBadge status={job.status} kind="job" />
        {job.hold === true && !['completed', 'failed', 'cancelled', 'rejected'].includes(job.status) && (
          <span className="px-2 py-0.5 rounded-md text-xs font-medium bg-amber-900/50 text-amber-400" title="Held: the scheduler skips this job until it is released">Held</span>
        )}
        <OutcomeCorrection key={`${job.id}:${job.status}`} jobId={job.id} status={job.status} />
      </div>
      <div className="mt-2 text-sm">
        <span className="text-xs text-[var(--brand-text-muted)] mr-1">Printer</span>
        <JobPrinterInfo job={job} pinnedPrinterName={pinnedPrinterName}
          filament={<FilamentToggle job={job} open={showFilament} onToggle={() => setShowFilament(v => !v)} />} />
      </div>
      {showFilament && job.printer_id != null && (
        <div className="mt-2"><FilamentCheckPanel jobId={job.id} printerId={job.printer_id} /></div>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-[var(--brand-text-secondary)]">
        <PriorityBadge priority={job.priority} />
        <span className="font-mono">{formatHours(job.duration_hours)}</span>
        {job.scheduled_start && <span className="font-mono text-xs">{format(new Date(job.scheduled_start), 'MMM d HH:mm')}</span>}
      </div>
      <JobActions job={job} onAction={onAction} isDispatching={isDispatching} dispatchFenced={dispatchFenced} correctionPrincipal={correctionPrincipal}
        className="mt-2 flex flex-wrap items-center gap-1 [&_button]:min-h-11 [&_button]:min-w-11" />
      {/* The table reorders by drag, which touch screens do not have: the card moves a job one place. */}
      {onMove && (job.status === 'pending' || job.status === 'scheduled') && (
        <div className="mt-1 flex items-center gap-1 [&_button]:min-h-11 [&_button]:min-w-11">
          <Button variant="ghost" size="icon" icon={ChevronUp} onClick={() => onMove(job.id, -1)} className="text-[var(--brand-text-secondary)]" aria-label={`Move ${displayName(job)} up in the queue`} />
          <Button variant="ghost" size="icon" icon={ChevronDown} onClick={() => onMove(job.id, 1)} className="text-[var(--brand-text-secondary)]" aria-label={`Move ${displayName(job)} down in the queue`} />
        </div>
      )}
    </li>
  )
}

export function JobCardList({ jobs, isLoading, emptyText, selectedIds, onToggleSelect, onSelectAll, allSelected, onAction, dispatchFencedIds, dispatchingId, correctionPrincipal, printers, extraJob, highlightId, onMove }: {
  jobs: any[]; isLoading: boolean; emptyText: string; selectedIds: Set<number>; onToggleSelect: (id: number) => void; onSelectAll: () => void; allSelected: boolean
  onAction: (...args: any[]) => void; dispatchFencedIds?: Set<number>; dispatchingId?: number | null; correctionPrincipal?: any; printers: any[]
  // A job opened by link that is not in the loaded list is shown first, like the table's extra row.
  extraJob?: any; highlightId?: number | null; onMove?: (jobId: number, direction: -1 | 1) => void
}) {
  const extra = extraJob ? (
    <ul aria-label="Linked job">
      <JobCard key={`deep-${extraJob.id}`} job={extraJob} highlight onAction={onAction} isSelected={selectedIds.has(extraJob.id)} onToggleSelect={onToggleSelect} correctionPrincipal={correctionPrincipal}
        dispatchFenced={dispatchFencedIds?.has(extraJob.id)} isDispatching={dispatchingId === extraJob.id}
        pinnedPrinterName={extraJob.pinned_printer_id != null ? printers.find(p => p.id === extraJob.pinned_printer_id)?.name : undefined} />
    </ul>
  ) : null
  if (isLoading) {
    return <>{extra}<PageState kind="loading" className="flex items-center justify-center gap-2 px-4 py-8 text-center text-[var(--brand-text-muted)] text-sm"><RefreshCw size={14} className="animate-spin" />Loading...</PageState></>
  }
  if (jobs.length === 0) {
    return <>{extra}<div className="px-4 py-8 text-center text-[var(--brand-text-muted)] text-sm">{emptyText}</div></>
  }
  return (
    <div>
      {extra}
      <div className="flex items-center gap-2 px-3 py-2 bg-[var(--brand-content-bg)] border-b border-[var(--brand-card-border)] text-xs text-[var(--brand-text-secondary)]">
        <CheckTarget touch><input type="checkbox" checked={allSelected} onChange={onSelectAll} className="h-6 w-6 shrink-0 rounded border-[var(--brand-text-muted)]" aria-label="Select all jobs" /></CheckTarget>
        <span>Select all</span>
      </div>
      <ul aria-label="Jobs list">
        {jobs.map(job => (
          <JobCard key={job.id} job={job} highlight={highlightId === job.id} onMove={onMove} onAction={onAction} isSelected={selectedIds.has(job.id)} onToggleSelect={onToggleSelect}
            correctionPrincipal={correctionPrincipal} dispatchFenced={dispatchFencedIds?.has(job.id)} isDispatching={dispatchingId === job.id}
            pinnedPrinterName={job.pinned_printer_id != null ? printers.find(p => p.id === job.pinned_printer_id)?.name : undefined} />
        ))}
      </ul>
    </div>
  )
}

export default function JobRow({ job, onAction, dragProps, isSelected, onToggleSelect, isDispatching, dispatchFenced, correctionPrincipal, pinnedPrinterName, highlight }: { job: any; onAction: (...args: any[]) => void; dragProps?: any; isSelected: boolean; onToggleSelect: (id: number) => void; isDispatching?: boolean; dispatchFenced?: boolean; correctionPrincipal?: any; pinnedPrinterName?: string; highlight?: boolean }) {
  const [showFilament, setShowFilament] = useState(false)
  return (
    <>
    <tr className={clsx(
          'border-b border-[var(--brand-card-border)] hover:bg-[var(--brand-input-bg)]/50 even:bg-[var(--brand-content-bg)]/40',
          highlight && 'ring-2 ring-inset ring-[var(--brand-primary)]',
          job.due_date && new Date(job.due_date) < new Date() && !['completed','cancelled','failed'].includes(job.status) && 'bg-red-950/30 border-l-2 border-l-red-500'
        )}
        data-job-id={job.id}
        {...(dragProps || {})}>
      <td className="px-2 py-3 w-10">
        <div className="flex items-center gap-1">
          {dragProps && (
            <GripVertical size={14} className="text-[var(--brand-text-muted)] shrink-0 cursor-grab" title="Drag to reorder" />
          )}
          <CheckTarget><input type="checkbox" checked={isSelected} onChange={() => onToggleSelect(job.id)} className="h-6 w-6 shrink-0 rounded border-[var(--brand-text-muted)]" aria-label={`Select job ${displayName(job)}`} /></CheckTarget>
          {job.queue_position != null && ['pending', 'scheduled'].includes(job.status) && (
            <span className="text-xs font-mono whitespace-nowrap text-[var(--brand-text-muted)]" title="Queue position">{`Queue ${job.queue_position + 1}`}</span>
          )}
        </div>
      </td>
      <td className="px-3 md:px-4 py-3">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <StatusBadge status={job.status} kind="job" />
          {job.hold === true && !['completed', 'failed', 'cancelled', 'rejected'].includes(job.status) && (
            <span className="px-2 py-0.5 rounded-md text-xs font-medium bg-amber-900/50 text-amber-400" title="Held: the scheduler skips this job until it is released">Held</span>
          )}
          <OutcomeCorrection key={`${job.id}:${job.status}`} jobId={job.id} status={job.status} />
        </div>
      </td>
      <td className="px-3 md:px-4 py-3">
        <JobItemDetails job={job} />
      </td>
      <td className="px-3 md:px-4 py-3">
        <PriorityBadge priority={job.priority} />
      </td>
      <td className="px-3 md:px-4 py-3 text-sm">
        <JobPrinterInfo job={job} pinnedPrinterName={pinnedPrinterName}
          filament={<FilamentToggle job={job} open={showFilament} onToggle={() => setShowFilament(v => !v)} />} />
      </td>
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
        <JobActions job={job} onAction={onAction} isDispatching={isDispatching} dispatchFenced={dispatchFenced} correctionPrincipal={correctionPrincipal} />
      </td>
    </tr>
    {showFilament && job.printer_id != null && (
      <tr className="border-b border-[var(--brand-card-border)]">
        <td colSpan={9} className="px-3 md:px-4 pb-3">
          <FilamentCheckPanel jobId={job.id} printerId={job.printer_id} />
        </td>
      </tr>
    )}
    </>
  )
}
