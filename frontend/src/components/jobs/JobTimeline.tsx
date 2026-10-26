import { useQuery } from '@tanstack/react-query'
import { jobCompletion } from '../../api/jobCompletion'
import { formatDate } from '../../utils/shared'
export default function JobTimeline({jobId}:{jobId:number}) {
  const {data,isLoading,error}=useQuery({queryKey:['job-timeline',jobId],queryFn:()=>jobCompletion.timeline(jobId)})
  return <section aria-label="Job timeline" className="space-y-2">
    <h3 className="font-medium">Job timeline</h3>
    {isLoading&&<p role="status">Loading history…</p>}
    {error&&<p role="alert">Could not load job history.</p>}
    {data?.events.length===0&&<p>No recorded history.</p>}
    <ol className="space-y-2">{data?.events.map(event=><li key={event.id} className="text-sm break-words">
      <time dateTime={event.occurred_at}>{formatDate(event.occurred_at)}</time> · {event.kind.replace(/_/g,' ')}{event.code?` · ${event.code}`:''}
    </li>)}</ol>
  </section>
}
