import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { jobs } from '../../api'
import { canDo } from '../../permissions'
import type { Job } from '../../types'
import { Button, Modal } from '../ui'

export default function JobNoStart({ job, inTimeline = false }: { job: Job; inTimeline?: boolean }) {
  if (!job.hold || !['pending', 'scheduled'].includes(job.status) || !job.no_start_state || !canDo('jobs.cancel')) return null
  if (job.no_start_state === 'recorded') return <p className="text-sm" role="status">
    Start check recorded. You can now cancel this job.{' '}
    {inTimeline && <a className="underline" href={`/jobs?job=${job.id}`}>Open job</a>}
  </p>
  return <CheckStart jobId={job.id} />
}

function CheckStart({ jobId }: { jobId: number }) {
  const client = useQueryClient()
  const [open, setOpen] = useState(false)
  const [checked, setChecked] = useState(false)
  const save = useMutation({
    mutationFn: () => jobs.recordNoStart(jobId),
    onSuccess: () => {
      setOpen(false)
      client.invalidateQueries({ queryKey: ['jobs'] })
      client.invalidateQueries({ queryKey: ['job', jobId] })
    },
  })
  return <div className="text-sm">
    <p>A start was requested and did not begin</p>
    <Button size="sm" variant="tertiary" onClick={() => { setChecked(false); save.reset(); setOpen(true) }}>It did not start</Button>
    <Modal isOpen={open} onClose={() => { if (!save.isPending) setOpen(false) }} title={`Check the start of job ${jobId}`} size="sm">
      <div className="space-y-4">
        <label className="flex items-start gap-2">
          <input type="checkbox" checked={checked} onChange={e => setChecked(e.target.checked)} />
          I checked the printer for each start request. This job did not start and is not printing.
        </label>
        {save.isError && <p role="alert">{save.error.message}</p>}
        <Button disabled={!checked || save.isPending} onClick={() => save.mutate()}>Record check</Button>
      </div>
    </Modal>
  </div>
}
