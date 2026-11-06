import { useState } from 'react'
import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query'
import { educationClasses, type PortfolioItem } from '../../api/educationClasses'
import { canDo } from '../../permissions'
import { Button, Input, Select, Card } from '../ui'

export default function StudentPortfolio({ audience, assignmentId, assessable = false }: {
  audience: string; assignmentId?: string; assessable?: boolean
}) {
  const query = useInfiniteQuery({ queryKey: ['class-portfolio', audience, assignmentId],
    queryFn: ({ pageParam }) => educationClasses.portfolio(assignmentId, pageParam), initialPageParam: 0,
    getNextPageParam: page => page.next_offset ?? undefined })
  if (query.isLoading) return <p>Loading portfolio…</p>
  if (query.isError) return <p role="alert">Unable to load portfolio. <button onClick={() => query.refetch()}>Retry</button></p>
  const items = query.data?.pages.flatMap(p => p.items) || []
  return <section className="space-y-4" aria-label={assignmentId ? 'Assignment submissions' : 'My portfolio'}>
    {!items.length && <p>No submissions yet.</p>}
    {items.map(item => <PortfolioCard key={`${item.submission_id}:${item.assessment_revision}`} item={item}
      assessable={assessable && canDo('education.classes_assess')} />)}
    {query.hasNextPage && <Button disabled={query.isFetchingNextPage} onClick={() => query.fetchNextPage()}>More submissions</Button>}
  </section>
}
function PortfolioCard({ item, assessable }: { item: PortfolioItem; assessable: boolean }) {
  const [imageMissing, setImageMissing] = useState(false)
  const [playing, setPlaying] = useState<number | null>(null)
  const [mediaError, setMediaError] = useState(false)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const client = useQueryClient()
  return <Card className="space-y-3">
    <div className="flex gap-4">
      {imageMissing ? <span className="text-sm">Thumbnail unavailable</span> : <img src={item.thumbnail_url} alt={`Preview of ${item.item_name}`} className="h-24 w-24 object-contain" onError={() => setImageMissing(true)} />}
      <div><h3 className="font-semibold">{item.item_name}</h3><p>{item.class_title || 'Unassigned'} {item.term} · {item.assignment_title}</p>
        <p>{item.student_username} · Print: {item.status} · Assessment: {item.learning_status || 'Not assessed'}</p></div>
    </div>
    {!assessable && <><p>Grade: {item.grade || 'Not graded'}</p><p className="whitespace-pre-wrap">{item.feedback || 'No feedback yet.'}</p></>}
    {item.timelapse_ids.map(id => <Button key={id} variant="secondary" onClick={() => { setPlaying(id); setMediaError(false) }}>Watch timelapse {id}</Button>)}
    {playing && <div><Button variant="secondary" onClick={() => setPlaying(null)}>Close video</Button><video key={playing} className="w-full" controls src={`/api/timelapses/${playing}/stream`} onError={() => setMediaError(true)} />
      {mediaError && <p role="alert">Timelapse unavailable. Access and recording provenance are checked when playing.</p>}</div>}
    {assessable && item.assignment_id && <form className="space-y-3" onSubmit={async e => {
      e.preventDefault(); if (pending) return
      const data = new FormData(e.currentTarget)
      setPending(true); setError('')
      try { await educationClasses.assess(item.assignment_id!, item, { feedback: String(data.get('feedback') || ''), grade: String(data.get('grade') || ''), learning_status: String(data.get('status')) }); await client.invalidateQueries({ queryKey: ['class-portfolio'] }) }
      catch (err) { setError(err instanceof Error ? err.message : 'Unable to save assessment') }
      finally { setPending(false) }
    }}>
      <label className="block">Feedback<textarea name="feedback" className="block w-full rounded bg-[var(--brand-input-bg)] p-2" maxLength={10000} defaultValue={item.feedback || ''} /></label>
      <Input name="grade" label="Grade" maxLength={100} defaultValue={item.grade || ''} />
      <Select name="status" label="Assessment status" defaultValue={item.learning_status || 'submitted'}>
        <option value="submitted">Submitted</option><option value="reviewed">Reviewed</option><option value="needs_revision">Needs revision</option><option value="complete">Complete</option>
      </Select>
      {error && <p role="alert">{error} <button type="button" onClick={() => client.invalidateQueries({ queryKey: ['class-portfolio'] })}>Reload assessment</button></p>}
      <Button type="submit" disabled={pending}>{pending ? 'Saving…' : 'Save assessment'}</Button>
    </form>}
  </Card>
}
