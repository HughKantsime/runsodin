import { useQuery } from '@tanstack/react-query'
import { printers } from '../../api'
import { Card, Button } from '../ui'

export default function DispatchRecoveryPanel({ printerId }: { printerId: number }) {
  const query = useQuery({
    queryKey: ['dispatch-recovery', printerId],
    queryFn: () => printers.dispatchRecovery(printerId),
    enabled: Number.isInteger(printerId) && printerId > 0,
    retry: false,
  })
  const data = query.data
  return <Card className="mb-6 break-words">
    <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
      <h2 className="text-sm font-medium">Dispatch evidence</h2>
      <Button size="sm" variant="ghost" disabled={query.isFetching} onClick={() => query.refetch()}>Refresh evidence</Button>
    </div>
    {query.isError ? <p role="alert">Dispatch evidence is unavailable for this printer. Refresh to try again.</p>
      : query.isPending ? <p role="status">Loading dispatch evidence...</p>
      : !data ? <p>No dispatch evidence was returned.</p>
      : data.state === 'no_retained_claim' ? <p>No dispatch fence is currently retained. This does not confirm a successful print.</p>
      : <div className="space-y-2 text-sm">
        <p role="status">An unresolved dispatch fence is retained: {data.state}. Repeat dispatch is blocked.</p>
        <p>Scheduled job: {data.job_id ?? 'unavailable in this scope'}. {data.job ? `Recorded state: ${data.job.status}. ${data.job.held ? 'Held from scheduling.' : 'Not held from scheduling.'}` : 'Job details are unavailable in this scope.'}</p>
        <p>{data.receipt_present ? 'Admission receipt is retained.' : 'No admission receipt is linked.'}</p>
        <p>{data.observed_print ? `Linked print observation ${data.observed_print.id}: ${data.observed_print.status}. The fence remains unresolved.` : 'No matching print observation is linked. Do not infer completion.'}</p>
        {data.education && <p>Education submission {data.education.submission_id}: {data.education.status}. Reservation: {data.education.reservation_state || 'none recorded'}.</p>}
        <p>{data.guidance}</p>
        <p>Inspect the printer and stop any active dispatch sender before reconciliation. This screen cannot release a fence or retry a print.</p>
      </div>}
  </Card>
}
