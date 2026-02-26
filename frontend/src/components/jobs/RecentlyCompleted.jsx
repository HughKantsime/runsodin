import { CheckCircle, XCircle, History } from 'lucide-react'

export default function RecentlyCompleted({ jobs: jobList }) {
  const recent = jobList
    ?.filter(j => j.status === 'completed' || j.status === 'failed')
    .sort((a, b) => new Date(b.updated_at || b.created_at) - new Date(a.updated_at || a.created_at))
    .slice(0, 8)

  if (!recent || recent.length === 0) return null

  return (
    <div className="mt-6">
      <h3 className="text-sm font-display font-semibold text-farm-400 mb-3 flex items-center gap-2">
        <History size={14} />
        Recently Completed
      </h3>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-2">
        {recent.map(job => (
          <div key={job.id} className={`bg-farm-900 rounded-lg border p-3 ${
            job.status === 'failed' ? 'border-red-900/50' : 'border-farm-800'
          }`}>
            <div className="flex items-center justify-between mb-1">
              <span className="text-sm font-medium truncate">{job.item_name}</span>
              {job.status === 'completed'
                ? <CheckCircle size={14} className="text-green-400 flex-shrink-0" />
                : <XCircle size={14} className="text-red-400 flex-shrink-0" />
              }
            </div>
            <div className="text-xs text-farm-500">
              {job.printer?.name || 'Unknown printer'}
              {job.duration_hours ? ` · ${job.duration_hours}h` : ''}
            </div>
            {job.fail_reason && (
              <div className="text-xs text-red-400 mt-1 truncate">⚠ {job.fail_reason.replace(/_/g, ' ')}</div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
