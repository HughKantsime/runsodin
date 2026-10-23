import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { education } from '../../api'
import SubmissionSliceInfo from './SubmissionSliceInfo'
import { Button, Card, Select } from '../ui'
import type { EducationQueueItem, EducationPrinterQueue } from '../../api/education'

export default function EducationQueues({ kind, orgId, readOnly = false, audience }: {
  kind: 'jobs' | 'archive' | 'review'; orgId?: number | null; readOnly?: boolean; audience: string
}) {
  const client = useQueryClient()
  const [archived, setArchived] = useState<'all' | 'unarchived' | 'archived'>('all')
  const [center, setCenter] = useState('')
  const [error, setError] = useState('')
  const [dragged, setDragged] = useState<number | null>(null)
  const query = useQuery({ queryKey: ['education-queues', audience, orgId, kind, archived],
    queryFn: () => education.queue(kind, { orgId, archived }) })
  const refresh = async () => {
    await client.invalidateQueries({ queryKey: ['education-queues'] })
    await client.invalidateQueries({ queryKey: ['education-submissions'] })
  }
  const reorder = useMutation({ mutationFn: ({ group, ids }: { group: EducationPrinterQueue; ids: number[] }) =>
    education.reorderQueue(group.printer_id, { revision: group.revision, job_ids: ids }),
    onSuccess: async () => { setError(''); await refresh() },
    onError: async (failure: Error & { status?: number }) => {
      setError(failure.status === 409 ? 'Queue changed. Reloaded the latest order; try again.' : failure.message)
      await refresh()
    } })
  const move = (group: EducationPrinterQueue, from: number, to: number) => {
    const ids = group.items.filter(row => row.can_reorder).map(row => row.job_id)
    const start = ids.indexOf(from), end = ids.indexOf(to)
    if (start < 0 || end < 0 || start === end || reorder.isPending || readOnly) return
    ids.splice(end, 0, ids.splice(start, 1)[0])
    reorder.mutate({ group, ids })
  }
  const items = query.data?.items || []
  const centers = [...new Map(items.map(row => [row.cost_center_id, row.center_name])).entries()]
  const rowView = (row: EducationQueueItem) => <>
    <strong>{row.item_name}</strong>
    <p>{row.submitter_username} · {row.center_name} · {row.job_status}</p>
    {row.blocked_reason && <p role="status">{row.blocked_reason}</p>}
  </>
  return <div className="space-y-4">
    {error && <p role="alert">{error}</p>}
    {query.isLoading && <p>Loading queue…</p>}
    {query.error && <p role="alert">{query.error.message}</p>}
    <Button onClick={() => query.refetch()}>Refresh queue</Button>
    {kind === 'archive' && <>
      <Select aria-label="Archived work" value={archived} onChange={event => setArchived(event.target.value as typeof archived)}>
        <option value="all">All work</option><option value="unarchived">Unarchived</option><option value="archived">Archived</option>
      </Select>
      <Select aria-label="Archive cost center" value={center} onChange={event => setCenter(event.target.value)}>
        <option value="">All cost centers</option>{centers.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
      </Select>
    </>}
    {kind === 'jobs' ? query.data?.printers?.map(group => <section key={group.printer_id}>
      <h2>{group.printer_name || `Printer ${group.printer_id}`}</h2>
      {(['printing', 'committed', 'waiting'] as const).map(section => <div key={section}>
        <h3>{section === 'committed' ? 'Committed (in flight)' : section === 'printing' ? 'Printing' : 'Waiting'}</h3>
        {group.items.filter(row => row.section === section).map(row => {
          const waiting = group.items.filter(item => item.can_reorder)
          const index = waiting.findIndex(item => item.job_id === row.job_id)
          const enabled = row.can_reorder && !readOnly && !reorder.isPending
          return <Card key={row.id} draggable={enabled} onDragStart={() => setDragged(row.job_id)} onDragEnd={() => setDragged(null)}
            onDragOver={event => { if (enabled) event.preventDefault() }}
            onDrop={event => { event.preventDefault(); if (enabled && dragged !== null) move(group, dragged, row.job_id); setDragged(null) }}>
            {rowView(row)}
            {row.can_reorder && !readOnly && <>
              <Button aria-label={`Move ${row.item_name} up`} disabled={!enabled || index === 0} onClick={() => move(group, row.job_id, waiting[index - 1].job_id)}>Up</Button>
              <Button aria-label={`Move ${row.item_name} down`} disabled={!enabled || index === waiting.length - 1} onClick={() => move(group, row.job_id, waiting[index + 1].job_id)}>Down</Button>
            </>}
          </Card>
        })}
      </div>)}
    </section>) : centers.filter(([id]) => !center || String(id) === center).map(([id, name]) => <section key={id}>
      <h2>{name}</h2>
      {items.filter(row => row.cost_center_id === id).map(row => <Card key={row.id}>
        {rowView(row)}
        {kind === 'review' && <SubmissionSliceInfo id={row.id} revision={row.lifecycle_revision} orgId={orgId} audience={audience} />}
        {row.archived && <p>Archived · read only</p>}
        {row.can_archive && !readOnly && <Button onClick={async () => {
          try { await education.archiveSubmission(row.id, { revision: row.lifecycle_revision, command_id: crypto.randomUUID() }); setError(''); await refresh() }
          catch (failure) { setError(failure instanceof Error ? failure.message : 'Archive failed') }
        }}>Archive</Button>}
      </Card>)}
    </section>)}
    {query.data && !(kind === 'jobs' ? query.data.printers?.length : items.length) && <p>Queue is clear.</p>}
  </div>
}
