import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { printFilePlates } from '../../api/printFilePlates'
import { jobs } from '../../api/jobs'
import { canDo } from '../../permissions'
import { parseServerDate } from '../../utils/shared'

export default function CompletedFileJobs({ fileId }: { fileId: number }) {
  const [open, setOpen] = useState(false)
  const permitted = canDo('jobs.create')
  const client = useQueryClient()
  const history = useQuery({ queryKey: ['completed-file-jobs', fileId], queryFn: () => printFilePlates.completedJobs(fileId), enabled: permitted && open })
  const repeat = useMutation({
    mutationFn: (jobId: number) => jobs.repeat(jobId),
    onSuccess: () => client.invalidateQueries({ queryKey: ['jobs'] }),
  })
  if (!permitted) return null
  return <details className="mt-4 text-sm" onToggle={e => setOpen(e.currentTarget.open)}>
    <summary className="cursor-pointer">Print again</summary>
    {open && <div className="mt-2 space-y-2">
      {history.isLoading && <p role="status">Loading completed prints…</p>}
      {history.isError && <p role="alert">Could not load completed prints.</p>}
      {history.data?.length === 0 && <p>No completed jobs linked to this file.</p>}
      {history.data?.map(job => <div key={job.id} className="flex flex-wrap gap-2 items-center">
        <span>{job.printer_name} · {job.actual_end ? parseServerDate(job.actual_end).toLocaleString() : 'Date unknown'} · {job.plate_index == null ? 'Plate unknown' : `Plate ${job.plate_index + 1}`}</span>
        <span>{job.ams_mapping?.length ? 'Filament slots: ' + job.ams_mapping.map(n => n < 0 ? 'unused' : n + 1).join(', ') : 'No saved slot mapping'}</span>
        <button disabled={repeat.isPending} onClick={() => repeat.mutate(job.id)}>Repeat job #{job.id}</button>
      </div>)}
      {repeat.isError && <p role="alert">{repeat.error.message}</p>}
      {repeat.data && <p role="status">Queued again as job #{repeat.data.new_job_id}.{repeat.data.held_for_planned_load && ' On hold for filament loading.'}</p>}
    </div>}
  </details>
}
