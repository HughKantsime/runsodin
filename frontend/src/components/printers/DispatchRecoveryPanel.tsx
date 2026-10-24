import { useRef, useState, useEffect } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { printers, type DispatchRecoveryAction, type DispatchRecoveryInspection } from '../../api'
import { canDo } from '../../permissions'
import { Card, Button, Modal } from '../ui'

const checks = [
  ['printer_checked', 'I inspected this printer.'],
  ['sender_stopped_and_checked', 'I stopped and checked its dispatch sender.'],
  ['transport_and_device_queues_checked', 'I checked transport and device queues.'],
  ['no_pending_dispatch_can_arrive', 'I confirmed no pending dispatch can still arrive.'],
] as const
const states: Record<string, string> = {
  preparing: 'Dispatch preparation is retained', adapter_entered: 'A send may have begun',
  accepted: 'The printer accepted dispatch; evidence is retained', uncertain: 'The dispatch result is uncertain',
  education_reservation_retained: 'An Education reservation is retained',
}
const blockers: Record<string, string> = {
  hold_required: 'Hold the job before releasing its fence.',
  education_authority_unresolved: 'Education authority remains unresolved. Ask an authorized school administrator to repair its lifecycle.',
  status_unavailable: 'Fresh monitor telemetry is unavailable. Wait for the monitor, then refresh.',
  active_observation: 'The printer or a print observation is active. Investigate and retain the fence.',
  sender_quiescence_unverified: 'A sender or power operation remains unresolved. Investigate and retain the fence.',
  authority_unavailable: 'Job authority is unavailable in your scope.',
}

export default function DispatchRecoveryPanel({ printerId }: { printerId: number }) {
  const client = useQueryClient()
  const submitting = useRef(false)
  const refreshButton = useRef<HTMLButtonElement>(null)
  const activePrinter = useRef(printerId)
  activePrinter.current = printerId
  const [confirmation, setConfirmation] = useState<{ action: DispatchRecoveryAction; snapshot: DispatchRecoveryInspection } | null>(null)
  const [attested, setAttested] = useState<Record<string, boolean>>({})
  const [cancelIntent, setCancelIntent] = useState(false)
  const [notice, setNotice] = useState('')
  const query = useQuery({
    queryKey: ['dispatch-recovery', printerId], queryFn: () => printers.dispatchRecovery(printerId),
    enabled: Number.isInteger(printerId) && printerId > 0, retry: false, staleTime: 0,
    refetchOnMount: 'always',
  })
  useEffect(() => { setConfirmation(null); setNotice(''); setAttested({}); setCancelIntent(false) }, [printerId])
  const mutation = useMutation({
    retry: false,
    mutationFn: async ({ action, snapshot, targetId }: { action: DispatchRecoveryAction; snapshot: DispatchRecoveryInspection; targetId: number }) => {
      const body: Record<string, unknown> = { attempt_handle: snapshot.attempt_handle, snapshot_handle: snapshot.snapshot_handle,
        reason_category: action === 'release' ? 'verified_no_pending_dispatch' : 'investigation' }
      if (action === 'reconcile-retain') body.education_action = cancelIntent ? 'request_unbound_cancellation' : 'none'
      if (action === 'release') body.attestation = { version: 1, ...Object.fromEntries(checks.map(([key]) => [key, attested[key] === true])) }
      return printers.recoverDispatch(targetId, action, body)
    },
    onSuccess: async (result, { targetId }) => {
      if (activePrinter.current === targetId) { setNotice(result.outcome === 'released' ? 'Fence released; job remains held. No retry was queued.' : result.outcome === 'retained' ? 'Decision recorded; fence retained.' : 'Job held. An already admitted command cannot be stopped by hold.')
      setConfirmation(null) }
      await Promise.all([['dispatch-recovery', targetId], ['jobs'], ['printers'], ['printer', targetId]].map(queryKey => client.invalidateQueries({ queryKey })))
    },
    onError: async (_error, { targetId }) => {
      if (activePrinter.current === targetId) { setNotice('The decision could not be confirmed. Evidence is being refreshed. No action was retried; inspect the current fence before deciding again.')
      setConfirmation(null) }
      // GET only after every error, including timeout/503/unknown commit. On
      // remount we also GET rather than replaying any prior POST.
      await client.fetchQuery({ queryKey: ['dispatch-recovery', targetId], queryFn: () => printers.dispatchRecovery(targetId), staleTime: 0, retry: false }).catch(() => undefined)
    },
    onSettled: () => { submitting.current = false },
  })
  useEffect(() => {
    if (notice && !mutation.isPending) refreshButton.current?.focus()
  }, [notice, mutation.isPending])
  const data = query.data
  const canWrite = canDo('printers.dispatch_recovery')
  function open(action: DispatchRecoveryAction) {
    if (!data || mutation.isPending) return
    setAttested({}); setCancelIntent(false); setConfirmation({ action, snapshot: data })
  }
  function submit() {
    if (!confirmation || submitting.current) return
    submitting.current = true
    mutation.mutate({ ...confirmation, targetId: printerId })
  }
  return <Card className="mb-6 min-w-0 break-words">
    <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
      <h2 className="text-sm font-medium">Dispatch recovery</h2>
      <Button ref={refreshButton} size="sm" variant="ghost" disabled={query.isFetching || mutation.isPending} onClick={() => query.refetch()}>Refresh evidence</Button>
    </div>
    {notice && <p role="status" className="mb-3">{notice}</p>}
    {query.isError ? <p role="alert">Dispatch evidence is unavailable for this printer. Refresh to try again.</p>
      : query.isPending ? <p role="status">Loading dispatch evidence...</p>
      : !data ? <p>No dispatch evidence was returned.</p>
      : data.state === 'no_retained_claim' ? <p>No dispatch fence is currently retained. Missing evidence does not confirm a successful print or give permission to retry.</p>
      : <div className="space-y-3 text-sm">
        <p role="status">{states[data.state] || 'Dispatch evidence needs investigation'}. Repeat dispatch is blocked.</p>
        <p>Printer {printerId}. Scheduled job: {data.job_id ?? 'unavailable in this scope'}. {data.job ? `Recorded state: ${data.job.status}. ${data.job.held ? 'Held from scheduling.' : 'Not held from scheduling.'}` : 'Job details are unavailable in this scope.'}</p>
        <p>{data.receipt_present ? 'Admission receipt is retained.' : 'No admission receipt is linked.'}</p>
        <p>{data.observed_print ? `Linked print observation ${data.observed_print.id}: ${data.observed_print.status}.` : 'No matching print observation is linked. Do not infer completion.'}</p>
        {data.education && <p>Education submission {data.education.submission_id}: {data.education.status}. Reservation: {data.education.reservation_state || 'none recorded'}.</p>}
        <p>Hold stops future admission but cannot stop an admitted command. Idle status, silence and elapsed time do not prove safety.</p>
        <p>ODIN cannot see non-ODIN senders such as Bambu Studio or a print queued on the device. The operator attestation covers these.</p>
        {(data.blockers || []).map(code => <p key={code}>{blockers[code] || 'Evidence changed or a prerequisite is unresolved. Refresh and investigate.'}</p>)}
        {canWrite && data.attempt_handle && <div className="flex flex-wrap gap-2">
          {data.actions?.hold && <Button disabled={mutation.isPending} onClick={() => open('hold')}>Hold job</Button>}
          {data.actions?.reconcile_retain && <Button disabled={mutation.isPending} onClick={() => open('reconcile-retain')}>Record decision and retain fence</Button>}
          <Button disabled={!data.actions?.release || mutation.isPending} onClick={() => open('release')}>Release fence</Button>
        </div>}
      </div>}
    <Modal isOpen={!!confirmation} onClose={() => { if (!mutation.isPending) setConfirmation(null) }} title={confirmation?.action === 'release' ? 'Confirm fence release' : 'Confirm recovery decision'} size="md">
      {confirmation && <div className="p-4 space-y-4 break-words">
        <p>Printer {printerId}, job {confirmation.snapshot.job_id}. {states[confirmation.snapshot.state] || 'Retained dispatch evidence'}.</p>
        {confirmation.action === 'release' ? <>
          <p>The job will remain held. No retry will be queued. ODIN cannot see non-ODIN senders such as Bambu Studio or a print queued on the device. Confirm each physical and queue check below.</p>
          <fieldset className="space-y-3"><legend className="font-medium mb-2">Required operator attestation</legend>
            {checks.map(([key,label]) => <label key={key} className="flex items-start gap-2"><input type="checkbox" checked={!!attested[key]} disabled={mutation.isPending} onChange={e => setAttested({ ...attested, [key]: e.target.checked })} /><span>{label}</span></label>)}
          </fieldset>
        </> : <p>The job will be held. The fence, receipts, observations, assignment and accounting will remain. Hold cannot retract an admitted command.</p>}
        {confirmation.action === 'reconcile-retain' && confirmation.snapshot.education && ['reserved','awaiting'].includes(confirmation.snapshot.education.reservation_state || '') && <label className="flex gap-2"><input type="checkbox" checked={cancelIntent} onChange={e => setCancelIntent(e.target.checked)} />Record a request to cancel unbound Education dispatch permission. This request retains the reservation and changes no print status.</label>}
        <div className="flex flex-wrap gap-2">
          <Button disabled={mutation.isPending || (confirmation.action === 'release' && !checks.every(([key]) => attested[key]))} onClick={submit}>Confirm {confirmation.action === 'release' ? 'release' : confirmation.action === 'hold' ? 'hold' : 'retain'}</Button>
          <Button variant="ghost" disabled={mutation.isPending} onClick={() => setConfirmation(null)}>Back</Button>
        </div>
      </div>}
    </Modal>
  </Card>
}
