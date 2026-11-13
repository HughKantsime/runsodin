import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { jobs, approveJob, rejectJob, resubmitJob, bulkOps } from '../api'
import toast from 'react-hot-toast'


const REFUSAL_NEXT_STEP: Record<string, string> = {
  ams_mapping_required: 'To fix it: open the job with the pencil button and choose a filament slot for each colour.',
  printer_maintenance_hold: 'To fix it: release the maintenance hold on the printer page.',
  printer_busy: 'Wait for the printer to finish, then try again.',
}

/** A refusal arrives as "some_code: sentence". Show the sentence, and where to fix it when known. */
export function plainRefusal(message: string | null | undefined): string {
  if (!message) return ''
  const match = /^([a-z][a-z0-9_]{2,60}): ([\s\S]+)$/.exec(message.trim())
  if (!match) return message.trim()
  const next = REFUSAL_NEXT_STEP[match[1]]
  return next ? `${match[2].trim()} ${next}` : match[2].trim()
}

export function useJobMutations() {
  const queryClient = useQueryClient()
  const [dispatchFencedIds, setDispatchFencedIds] = useState<Set<number>>(() => new Set())
  const inv = () => queryClient.invalidateQueries({ queryKey: ['jobs'] })

  const runSchedulerMut = useMutation({
    mutationFn: () => import('../api').then(m => m.scheduler.run()),
    onSuccess: () => { inv(); queryClient.invalidateQueries({ queryKey: ['stats'] }); toast.success('Scheduler run complete') },
    onError: (err) => toast.error('Scheduler failed: ' + (err.message || 'Unknown error')),
  })

  const createJob = useMutation({
    mutationFn: jobs.create,
    onSuccess: () => { inv(); toast.success('Job created') },
    onError: (err) => toast.error('Create job failed: ' + err.message),
  })

  const updateJob = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Parameters<typeof jobs.update>[1] }) => jobs.update(id, data),
    onSuccess: () => { inv(); toast.success('Job updated') },
    onError: (err) => toast.error('Update job failed: ' + err.message),
  })

  const startJob = useMutation({
    mutationFn: jobs.start,
    onSuccess: () => { inv(); toast.success('Job started') },
    onError: (err) => toast.error('Start job failed: ' + err.message),
  })

  const completeJob = useMutation({
    mutationFn: jobs.complete,
    onSuccess: () => { inv(); toast.success('Job completed') },
    onError: (err) => toast.error('Complete job failed: ' + err.message),
  })

  const cancelJob = useMutation({
    mutationFn: jobs.cancel,
    onSuccess: () => { inv(); toast.success('Job cancelled') },
    onError: (err) => toast.error('Cancel job failed: ' + err.message),
  })

  const failJob = useMutation({
    mutationFn: jobs.fail,
    onSuccess: () => { inv(); toast.success('Job marked failed') },
    onError: (err) => toast.error('Mark job failed failed: ' + err.message),
  })

  const deleteJob = useMutation({
    mutationFn: jobs.delete,
    onSuccess: () => { inv(); toast.success('Job deleted') },
    onError: (err) => toast.error('Delete job failed: ' + err.message),
  })

  const repeatJob = useMutation({
    mutationFn: async (jobId: number) => jobs.repeat(jobId),
    onSuccess: (result) => {
      inv()
      toast.success(result.held_for_planned_load
        ? 'Job duplicated and held for planned filament loading. Review the job’s planned load before releasing.'
        : 'Job duplicated')
    },
    onError: (err) => toast.error('Repeat job failed: ' + err.message),
  })

  const dispatchJob = useMutation({
    mutationFn: jobs.dispatch,
    retry: false,
    onSuccess: (result, id) => {
      setDispatchFencedIds(previous => new Set(previous).add(id))
      inv()
      if (['refused', 'needs_reconciliation'].includes(result.status?.phase || '')) toast.error(plainRefusal(result.status?.message) || 'Dispatch refused. Review the printer before trying again.', { duration: 15000 })
      else if (result.confirmation === 'accepted' || result.status?.phase === 'start_accepted') toast.success('Start accepted. Waiting for printer progress.')
      else if (result.status?.phase === 'layer_observed') toast('Printer layer progress was observed for this attempt.')
      else toast('Dispatch is unconfirmed. Check the printer and recorded attempt before trying again.', { duration: 10000 })
    },
    onError: (err) => toast.error('Not sent: ' + (plainRefusal(err.message) || 'Unknown error'), { duration: 15000 }),
  })

  const approveJobMut = useMutation({
    mutationFn: approveJob,
    onSuccess: () => { inv(); toast.success('Job approved') },
    onError: (err) => toast.error('Approve job failed: ' + err.message),
  })

  const rejectJobMut = useMutation({
    mutationFn: ({ jobId, reason }: { jobId: number; reason: string }) => rejectJob(jobId, reason),
    onSuccess: () => { inv(); toast.success('Job rejected') },
    onError: (err) => toast.error('Reject job failed: ' + err.message),
  })

  const resubmitJobMut = useMutation({
    mutationFn: resubmitJob,
    onSuccess: () => { inv(); toast.success('Job resubmitted') },
    onError: (err) => toast.error('Resubmit job failed: ' + err.message),
  })

  const bulkAction = useMutation({
    mutationFn: ({ ids, action, extra }) => bulkOps.jobs(ids, action, extra),
    onSuccess: (_, vars) => { inv(); toast.success(`Bulk ${vars.action} completed`) },
    onError: (err, vars) => toast.error(`Bulk ${vars.action} failed: ${err.message}`),
  })

  return {
    runSchedulerMut,
    createJob,
    updateJob,
    startJob,
    completeJob,
    cancelJob,
    failJob,
    deleteJob,
    repeatJob,
    dispatchJob,
    dispatchFencedIds,
    approveJobMut,
    rejectJobMut,
    resubmitJobMut,
    bulkAction,
    queryClient,
  }
}
