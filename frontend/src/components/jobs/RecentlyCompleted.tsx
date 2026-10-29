import { displayName } from '../../utils/displayName'
import { useEffect, useState } from 'react'
import { CheckCircle, XCircle, History, AlertTriangle, ImageOff } from 'lucide-react'
import type { RecentTerminalJob } from '../../types'

function Thumbnail({ job }: { job: RecentTerminalJob }) {
  const [failed, setFailed] = useState(false)
  useEffect(() => setFailed(false), [job.thumbnail_data])
  const safe = job.thumbnail_data?.startsWith('data:image/png;base64,') || job.thumbnail_data?.startsWith('data:image/jpeg;base64,')
  return safe && !failed
    ? <img src={job.thumbnail_data!} alt={`Thumbnail for ${displayName(job)}`} className="w-16 h-16 object-contain rounded bg-[var(--brand-content-bg)] shrink-0" onError={() => setFailed(true)} />
    : <div className="w-16 h-16 rounded bg-[var(--brand-content-bg)] flex items-center justify-center shrink-0" aria-label="Thumbnail unavailable"><ImageOff size={20} className="text-[var(--brand-text-muted)]" /></div>
}

function duration(seconds: number | null) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return 'Duration unknown'
  if (seconds === 0) return '0s'
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  return `${Number((seconds / 3600).toFixed(2))}h`
}

export default function RecentlyCompleted({ jobs = [], loading = false, error = false, onRetry }: { jobs?: RecentTerminalJob[]; loading?: boolean; error?: boolean; onRetry?: () => void }) {
  return <section className="mt-6" aria-label="Recent completed and failed jobs">
    <h3 className="text-sm font-display font-semibold text-[var(--brand-text-secondary)] mb-3 flex items-center gap-2"><History size={14} />Recently Completed</h3>
    {loading ? <p role="status" className="text-sm text-[var(--brand-text-muted)]">Loading recent jobs…</p>
      : error ? <div role="alert" className="text-sm text-red-400">Recent jobs unavailable. {onRetry && <button type="button" onClick={() => onRetry()} className="underline">Retry</button>}</div>
      : jobs.length === 0 ? <p className="text-sm text-[var(--brand-text-muted)]">No completed or failed jobs.</p>
      : <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-2">{jobs.map(job => <article key={job.id} className={`bg-[var(--brand-card-bg)] rounded-md border p-3 flex gap-3 ${job.status === 'failed' ? 'border-red-900/50' : 'border-[var(--brand-card-border)]'}`}>
        <Thumbnail job={job} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between mb-1 gap-1"><span className="text-sm font-medium truncate" title={displayName(job)}>{displayName(job)}</span>{job.status === 'completed' ? <CheckCircle size={14} className="text-green-400 shrink-0" aria-label="Completed" /> : <XCircle size={14} className="text-red-400 shrink-0" aria-label="Failed" />}</div>
          <div className="text-xs text-[var(--brand-text-muted)]">{job.printer_name || 'Printer unavailable'} · {duration(job.duration_seconds)}</div>
          {job.model_name && <div className="text-xs text-[var(--brand-text-muted)] truncate">{job.model_name}</div>}
          <div className="text-xs text-[var(--brand-text-muted)]">{job.actual_end ? <time dateTime={job.actual_end}>{new Date(job.actual_end).toLocaleString()}</time> : 'Completion time unknown'}</div>
          {job.fail_reason && <div className="text-xs text-red-400 mt-1 truncate flex items-center gap-1" title={job.fail_reason.replace(/_/g, ' ')}><AlertTriangle size={10} className="shrink-0" />{job.fail_reason.replace(/_/g, ' ')}</div>}
        </div>
      </article>)}</div>}
  </section>
}
