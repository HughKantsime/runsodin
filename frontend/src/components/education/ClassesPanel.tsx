import { useState } from 'react'
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query'
import { educationClasses, type AssignmentRow, type ClassRow } from '../../api/educationClasses'
import type { EducationCostCenter } from '../../types'
import { canDo } from '../../permissions'
import { Button, Card, Input, Select } from '../ui'
import AssignmentForm from './AssignmentForm'
import StudentPortfolio from './StudentPortfolio'

export default function ClassesPanel({ audience, managedCenters }: { audience: string; managedCenters: EducationCostCenter[] }) {
  const [selected, setSelected] = useState('')
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const client = useQueryClient()
  const query = useInfiniteQuery({ queryKey: ['education-classes', audience], initialPageParam: 0,
    queryFn: ({ pageParam }) => educationClasses.list(pageParam), getNextPageParam: page => page.next_offset ?? undefined })
  const items = query.data?.pages.flatMap(p => p.items) || []
  const cls = items.find(c => c.id === selected)
  return <section className="space-y-4" aria-label="Classes and assignments">
    <p>Classes use the instructors and students assigned to their cost center. Administrators manage that roster under Cost centers &amp; rosters.</p>
    {managedCenters.length > 0 && canDo('education.classes_manage') && <Card><form className="grid gap-3 sm:grid-cols-2" onSubmit={async e => {
      e.preventDefault(); if (pending) return
      const form = e.currentTarget; const data = new FormData(form)
      setPending(true); setError('')
      try { const created = await educationClasses.create({ cost_center_id: Number(data.get('center')), title: String(data.get('title')), term: String(data.get('term')) });
        form.reset(); await client.invalidateQueries({ queryKey: ['education-classes'] }); setSelected(created.id) }
      catch (err) { setError(err instanceof Error ? err.message : 'Unable to create class') }
      finally { setPending(false) }
    }}>
      <Input name="title" label="Class title" required maxLength={200} /><Input name="term" label="Term" required maxLength={100} placeholder="Fall 2026" />
      <Select name="center" label="Class cost center" required>{managedCenters.map(c => <option value={c.id} key={c.id}>{c.name}</option>)}</Select>
      <Button type="submit" disabled={pending}>{pending ? 'Creating…' : 'Create class'}</Button>
      {error && <p role="alert">{error}</p>}
    </form></Card>}
    {query.isLoading && <p>Loading classes…</p>}
    {query.isError && <p role="alert">Unable to load classes. <button onClick={() => query.refetch()}>Retry</button></p>}
    {!query.isLoading && !items.length && !query.isError && <p>No classes assigned yet.</p>}
    <Select label="Select class" value={selected} onChange={e => setSelected(e.target.value)}><option value="">Choose a class</option>
      {items.map(c => <option value={c.id} key={c.id}>{c.title} · {c.term}{c.state === 'archived' ? ' (archived)' : ''}</option>)}</Select>
    {query.hasNextPage && <Button onClick={() => query.fetchNextPage()} disabled={query.isFetchingNextPage}>More classes</Button>}
    {cls && <ClassDetail key={cls.id} cls={cls} audience={audience} />}
  </section>
}
function ClassDetail({ cls, audience }: { cls: ClassRow; audience: string }) {
  const [edit, setEdit] = useState<AssignmentRow | 'new' | null>(null)
  const [chosen, setChosen] = useState('')
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const client = useQueryClient()
  const manageable = cls.can_manage && canDo('education.classes_manage')
  const roster = useQuery({ queryKey: ['class-roster', audience, cls.id], queryFn: () => educationClasses.roster(cls.id) })
  const assignments = useInfiniteQuery({ queryKey: ['class-assignments', audience, cls.id], initialPageParam: 0,
    queryFn: ({ pageParam }) => educationClasses.assignments(cls.id, pageParam), getNextPageParam: page => page.next_offset ?? undefined })
  const rows = assignments.data?.pages.flatMap(p => p.items) || []
  const assignment = rows.find(a => a.id === chosen)
  return <div className="space-y-4">
    <h2 className="text-lg font-semibold">{cls.title} · {cls.term}</h2>
    <details><summary>Class roster</summary>
      {roster.isLoading && <p>Loading roster…</p>}{roster.isError && <p role="alert">Unable to load roster.</p>}
      <ul>{roster.data?.items.map(r => <li key={`${r.user_id}:${r.role}`}>{r.username} · {r.role === 'manager' ? 'Instructor' : 'Student'}</li>)}</ul>
      {!cls.can_manage && <p>Other students’ names are private.</p>}
    </details>
    {manageable && <details><summary>Edit class or archive term</summary><form key={cls.revision} className="space-y-3" onSubmit={async e => {
      e.preventDefault(); if (pending) return
      const data = new FormData(e.currentTarget); setPending(true); setError('')
      try { await educationClasses.update(cls, { title: String(data.get('title')), term: String(data.get('term')), state: String(data.get('state')) });
        await client.invalidateQueries({ queryKey: ['education-classes'] }) }
      catch (err) { setError(err instanceof Error ? err.message : 'Unable to update class') }
      finally { setPending(false) }
    }}>
      <Input name="title" label="Edit class title" required defaultValue={cls.title} /><Input name="term" label="Edit term" required defaultValue={cls.term} />
      <Select name="state" label="Class state" defaultValue={cls.state}><option value="active">Active</option><option value="archived">Archived</option></Select>
      <Button type="submit" disabled={pending}>Save class</Button>{error && <p role="alert">{error} <button type="button" onClick={() => client.invalidateQueries({ queryKey: ['education-classes'] })}>Reload class</button></p>}
    </form></details>}
    {manageable && cls.state === 'active' && <Button onClick={() => setEdit('new')}>New assignment</Button>}
    {edit && manageable && cls.state === 'active' && <AssignmentForm key={edit === 'new' ? 'new' : `${edit.id}:${edit.revision}`} classId={cls.id} centerId={cls.cost_center_id} audience={audience}
      existing={edit === 'new' ? undefined : edit} onCancel={() => setEdit(null)} onSaved={() => { setEdit(null); client.invalidateQueries({ queryKey: ['class-assignments'] }) }} />}
    {assignments.isLoading && <p>Loading assignments…</p>}{assignments.isError && <p role="alert">Unable to load assignments. <button onClick={() => assignments.refetch()}>Retry</button></p>}
    {!assignments.isLoading && !assignments.isError && !rows.length && <p>No assignments yet.</p>}
    <Select label="Select assignment" value={chosen} onChange={e => setChosen(e.target.value)}><option value="">Choose an assignment</option>
      {rows.map(a => <option key={a.id} value={a.id}>{a.title}{a.state === 'closed' ? ' (closed)' : ''}</option>)}</Select>
    {assignments.hasNextPage && <Button onClick={() => assignments.fetchNextPage()} disabled={assignments.isFetchingNextPage}>More assignments</Button>}
    {assignment && <section className="space-y-3">
      <h3 className="font-semibold">{assignment.title}</h3><p className="whitespace-pre-wrap">{assignment.brief}</p>
      <p>Due: {assignment.due_at ? new Date(assignment.due_at).toLocaleString() : 'No deadline'}</p>
      <p>Limits: {assignment.max_print_seconds ? `${assignment.max_print_seconds / 60} minutes` : 'No time limit'} · {assignment.max_material_grams ? `${assignment.max_material_grams} g` : 'No weight limit'} · {assignment.submission_quota ?? 'No'} submission limit per student</p>
      <p>Materials: {assignment.allowed_materials.join(', ') || 'Any'}</p>
      {manageable && cls.state === 'active' && <Button variant="secondary" onClick={() => setEdit(assignment)}>Edit assignment</Button>}
      {cls.can_submit && cls.state === 'active' && assignment.state === 'active' && canDo('education.classes_submit') && <LinkSubmission key={assignment.id} assignment={assignment} cls={cls} audience={audience} />}
      <StudentPortfolio key={assignment.id} audience={audience} assignmentId={assignment.id} assessable={cls.can_manage} />
    </section>}
  </div>
}
function LinkSubmission({ assignment, cls, audience }: { assignment: AssignmentRow; cls: ClassRow; audience: string }) {
  const [selected, setSelected] = useState('')
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const client = useQueryClient()
  const mine = useInfiniteQuery({ queryKey: ['class-linkable', audience, cls.id], initialPageParam: 0,
    queryFn: ({ pageParam }) => educationClasses.portfolio(undefined, pageParam), getNextPageParam: page => page.next_offset ?? undefined })
  const items = mine.data?.pages.flatMap(p => p.items).filter(i => !i.assignment_id && i.status === 'submitted' && i.cost_center_id === cls.cost_center_id) || []
  return <Card className="space-y-3">
    <p>Use Submit print to upload to this class’s cost center, then link your pending print here. An upload remains unassigned until linking succeeds.</p>
    <Select label="Pending print for assignment" value={selected} onChange={e => setSelected(e.target.value)}><option value="">Choose your pending print</option>
      {items.map(i => <option key={i.submission_id} value={i.submission_id}>{i.item_name}</option>)}</Select>
    {mine.isLoading && <p>Loading pending prints…</p>}{mine.isError && <p role="alert">Unable to load your pending prints.</p>}
    <Button variant="secondary" onClick={() => mine.refetch()}>Refresh pending prints</Button>
    {mine.hasNextPage && <Button disabled={mine.isFetchingNextPage} onClick={() => mine.fetchNextPage()}>More pending prints</Button>}
    <Button disabled={pending || !items.some(i => String(i.submission_id) === selected)} onClick={async () => {
      const item = items.find(i => String(i.submission_id) === selected); if (!item || pending) return
      setPending(true); setError(''); setMessage('')
      try { const result = await educationClasses.link(assignment.id, item.submission_id, item.lifecycle_revision)
        const status = ['submitted', 'manual_review', 'review_unavailable'].includes(result.auto_approval)
          ? 'pending review' : result.auto_approval.replace(/_/g, ' ')
        setMessage(['approved', 'pending', 'scheduled', 'printing', 'completed'].includes(result.auto_approval)
          ? 'Linked and approved for printing.' : `Linked. Status: ${status}.`); setSelected('')
        await Promise.all(['class-linkable', 'class-portfolio', 'education-submissions', 'education-queues'].map(key => client.invalidateQueries({ queryKey: [key] }))) }
      catch (err) { setError(`${err instanceof Error ? err.message : 'Link failed'}. Reload to confirm whether this print was linked before retrying.`) }
      finally { setPending(false) }
    }}>{pending ? 'Linking…' : 'Link to assignment'}</Button>
    {error && <p role="alert">{error}</p>}{message && <p role="status">{message}</p>}
  </Card>
}
