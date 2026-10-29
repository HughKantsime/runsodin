import { displayName } from '../../utils/displayName'
import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Clock, Star, Layers, Thermometer, AlertTriangle } from 'lucide-react'
import { format, differenceInMinutes } from 'date-fns'
import clsx from 'clsx'
import { jobs, printJobs } from '../../api'
import JobTimeline from './JobTimeline'
import JobAmsMapping from './JobAmsMapping'

function parseUTC(dateStr) {
  if (!dateStr) return null
  if (!dateStr.endsWith('Z') && !dateStr.includes('+')) {
    dateStr = dateStr + 'Z'
  }
  return new Date(dateStr)
}

const statusColors = {
  pending: 'bg-status-pending',
  scheduled: 'bg-status-scheduled',
  printing: 'bg-status-printing',
  completed: 'bg-status-completed',
  failed: 'bg-status-failed',
}

export default function BlockDetail({ block }) {
  if (block.job_id) return <ScheduledJobDetail block={block} />
  if (block.mqtt_job_id) return <MqttJobDetail block={block} />
  return <p className="text-[var(--brand-text-muted)] text-sm">No details available.</p>
}

function ScheduledJobDetail({ block }) {
  const { data: job, isLoading } = useQuery({
    queryKey: ['job', block.job_id],
    queryFn: () => jobs.get(block.job_id),
    enabled: !!block.job_id,
  })

  if (isLoading) return <p className="text-[var(--brand-text-muted)] text-sm">Loading...</p>
  if (!job) return <p className="text-[var(--brand-text-muted)] text-sm">Job not found.</p>

  const durationMin = differenceInMinutes(parseUTC(block.end), parseUTC(block.start))
  const hours = Math.floor(durationMin / 60)
  const mins = durationMin % 60

  return (
    <div className="space-y-3">
      {/* Status + name */}
      <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[var(--brand-text-primary)] font-medium">{displayName(job.display_name || job.item_name ? job : block)}</span>
          <span className={clsx('px-2 py-0.5 rounded text-xs font-medium capitalize', statusColors[job.status] || 'bg-[var(--brand-card-border)]')}>{job.status}</span>
        </div>
        <p className="text-[var(--brand-text-secondary)] text-sm">Printer: {block.printer_name}</p>
      </div>

      <JobAmsMapping job={job} />
      <JobTimeline jobId={block.job_id} />
      {/* Color swatches */}
      {block.colors?.length > 0 && (
        <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
          <h4 className="text-[var(--brand-text-secondary)] text-xs uppercase tracking-wide mb-2">Colors</h4>
          <div className="flex gap-2 flex-wrap">
            {block.colors.map((c, i) => (
              <div key={i} className="w-6 h-6 rounded-full border border-[var(--brand-card-border)]" style={{ backgroundColor: c }} title={c} />
            ))}
          </div>
        </div>
      )}

      {/* Schedule & Duration */}
      <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
        <h4 className="text-[var(--brand-text-secondary)] text-xs uppercase tracking-wide mb-2">Schedule</h4>
        <div className="space-y-1.5 text-sm">
          <Row icon={<Clock size={14} />} label="Start" value={format(parseUTC(block.start), 'MMM d, HH:mm')} />
          <Row icon={<Clock size={14} />} label="End" value={format(parseUTC(block.end), 'MMM d, HH:mm')} />
          <Row label="Duration" value={hours > 0 ? `${hours}h ${mins}m` : `${mins}m`} />
        </div>
      </div>

      {/* Print time: slicer estimate alongside the actual (monitor/operator) times */}
      {(job.duration_hours != null || job.actual_start || job.actual_end) && (() => {
        const actualMin = job.actual_start && job.actual_end
          ? differenceInMinutes(parseUTC(job.actual_end), parseUTC(job.actual_start)) : null
        const fmt = (m) => { const h = Math.floor(m / 60), r = m % 60; return h > 0 ? `${h}h ${r}m` : `${r}m` }
        return (
          <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
            <h4 className="text-[var(--brand-text-secondary)] text-xs uppercase tracking-wide mb-2">Print Time</h4>
            <div className="space-y-1.5 text-sm">
              {job.duration_hours != null && (
                <Row label="Estimated" value={fmt(Math.round(job.duration_hours * 60))} />
              )}
              {job.actual_start && (
                <Row icon={<Clock size={14} />} label="Actual start" value={format(parseUTC(job.actual_start), 'MMM d, HH:mm')} />
              )}
              {job.actual_end && (
                <Row icon={<Clock size={14} />} label="Actual end" value={format(parseUTC(job.actual_end), 'MMM d, HH:mm')} />
              )}
              {actualMin != null && actualMin >= 0 && (
                <Row label="Actual duration" value={fmt(actualMin)} />
              )}
            </div>
          </div>
        )
      })()}

      {/* Job details */}
      <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
        <h4 className="text-[var(--brand-text-secondary)] text-xs uppercase tracking-wide mb-2">Details</h4>
        <div className="space-y-1.5 text-sm">
          {job.priority != null && (
            <Row icon={<Star size={14} />} label="Priority" value={job.priority} />
          )}
          {job.filament_type && (
            <Row icon={<Layers size={14} />} label="Filament" value={job.filament_type} />
          )}
          {job.due_date && (
            <Row label="Due" value={format(parseUTC(job.due_date), 'MMM d, yyyy')} />
          )}
          {job.match_score != null && (
            <Row label="Match score" value={`${Math.round(job.match_score * 100)}%`} />
          )}
        </div>
      </div>

      {/* Cost / Price */}
      {(job.estimated_cost != null || job.suggested_price != null) && (
        <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
          <h4 className="text-[var(--brand-text-secondary)] text-xs uppercase tracking-wide mb-2">Pricing</h4>
          <div className="space-y-1.5 text-sm">
            {job.estimated_cost != null && <Row label="Est. cost" value={`$${Number(job.estimated_cost).toFixed(2)}`} />}
            {job.suggested_price != null && <Row label="Sugg. price" value={`$${Number(job.suggested_price).toFixed(2)}`} />}
          </div>
        </div>
      )}

      {/* Failure info */}
      {job.status === 'failed' && (job.fail_reason || job.fail_notes) && (
        <div className="bg-red-900/30 rounded-md border border-red-800/50 p-4">
          <h4 className="text-red-400 text-xs uppercase tracking-wide mb-2 flex items-center gap-1.5">
            <AlertTriangle size={14} /> Failure
          </h4>
          <div className="space-y-1.5 text-sm">
            {job.fail_reason && <Row label="Reason" value={job.fail_reason} />}
            {job.fail_notes && <p className="text-[var(--brand-text-secondary)] mt-1">{job.fail_notes}</p>}
          </div>
        </div>
      )}

      {/* Notes */}
      {job.notes && (
        <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
          <h4 className="text-[var(--brand-text-secondary)] text-xs uppercase tracking-wide mb-2">Notes</h4>
          <p className="text-[var(--brand-text-secondary)] text-sm">{job.notes}</p>
        </div>
      )}
    </div>
  )
}

function MqttJobDetail({ block }) {
  const { data: pj, isLoading } = useQuery({
    queryKey: ['printJob', block.mqtt_job_id],
    queryFn: () => printJobs.get(block.mqtt_job_id),
    enabled: !!block.mqtt_job_id,
  })

  if (isLoading) return <p className="text-[var(--brand-text-muted)] text-sm">Loading...</p>
  if (!pj) return <p className="text-[var(--brand-text-muted)] text-sm">Print job not found.</p>

  const progress = pj.progress ?? pj.percent_complete ?? 0

  return (
    <div className="space-y-3">
      {/* Name + status */}
      <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[var(--brand-text-primary)] font-medium">{displayName(pj.display_name || pj.filename || pj.job_name ? pj : block)}</span>
          <span className={clsx('px-2 py-0.5 rounded text-xs font-medium capitalize', statusColors[pj.status] || 'bg-[var(--brand-card-border)]')}>{pj.status}</span>
        </div>
        <p className="text-[var(--brand-text-secondary)] text-sm">Printer: {block.printer_name}</p>
      </div>

      {/* Progress */}
      <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
        <h4 className="text-[var(--brand-text-secondary)] text-xs uppercase tracking-wide mb-2">Progress</h4>
        <div className="w-full bg-[var(--brand-input-bg)] rounded-full h-2.5 mb-2" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-label="Job progress" aria-valuenow={Math.round(Math.min(Number(progress) || 0, 100))}>
          <div
            className="bg-[var(--brand-primary)] h-2.5 rounded-full transition-all"
            style={{ width: `${Math.min(Number(progress), 100)}%` }}
          />
        </div>
        <span className="text-[var(--brand-text-secondary)] text-sm">{Math.round(Number(progress))}%</span>
      </div>

      {/* Details */}
      <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
        <h4 className="text-[var(--brand-text-secondary)] text-xs uppercase tracking-wide mb-2">Details</h4>
        <div className="space-y-1.5 text-sm">
          {pj.current_layer != null && pj.total_layers != null && (
            <Row icon={<Layers size={14} />} label="Layer" value={`${pj.current_layer} / ${pj.total_layers}`} />
          )}
          {pj.remaining_time != null && (
            <Row icon={<Clock size={14} />} label="Remaining" value={formatSeconds(pj.remaining_time)} />
          )}
          {pj.total_duration != null && (
            <Row icon={<Clock size={14} />} label="Total duration" value={formatSeconds(pj.total_duration)} />
          )}
        </div>
      </div>

      {/* Temperatures */}
      {(pj.bed_temp_target != null || pj.nozzle_temp_target != null) && (
        <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
          <h4 className="text-[var(--brand-text-secondary)] text-xs uppercase tracking-wide mb-2">Temperatures</h4>
          <div className="space-y-1.5 text-sm">
            {pj.bed_temp_target != null && (
              <Row icon={<Thermometer size={14} />} label="Bed target" value={`${pj.bed_temp_target}\u00B0C`} />
            )}
            {pj.nozzle_temp_target != null && (
              <Row icon={<Thermometer size={14} />} label="Nozzle target" value={`${pj.nozzle_temp_target}\u00B0C`} />
            )}
          </div>
        </div>
      )}

      {/* Error */}
      {pj.error_code && (
        <div className="bg-red-900/30 rounded-md border border-red-800/50 p-4">
          <h4 className="text-red-400 text-xs uppercase tracking-wide mb-2 flex items-center gap-1.5">
            <AlertTriangle size={14} /> Error
          </h4>
          <p className="text-[var(--brand-text-secondary)] text-sm">{pj.error_code}</p>
        </div>
      )}
    </div>
  )
}

function Row({ icon, label, value }: { icon?: ReactNode; label: ReactNode; value: ReactNode }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-[var(--brand-text-secondary)] flex items-center gap-1.5">
        {icon}{label}
      </span>
      <span className="text-[var(--brand-text-primary)]">{value}</span>
    </div>
  )
}

function formatSeconds(sec) {
  if (sec == null) return '--'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}
