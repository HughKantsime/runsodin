import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { educationProgramReports as api, type ProgramMark, type InstructorHours } from '../../api/educationProgramReports'
import { educationClasses, type ClassRow } from '../../api/educationClasses'
import { Button, Card, Input, Select } from '../ui'
import ProgramReportForm from './ProgramReportForm'

const today = () => new Date().toISOString().slice(0, 10)
export default function ProgramReporting({ cls, audience }: { cls: ClassRow; audience: string }) {
  const client = useQueryClient()
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const records = useQuery({ queryKey: ['program-records', audience, cls.id], queryFn: () => api.records(cls.id) })
  const roster = useQuery({ queryKey: ['class-roster', audience, cls.id], queryFn: () => educationClasses.roster(cls.id) })
  const action = async (work: () => Promise<unknown>) => {
    if (pending) return
    setPending(true); setError('')
    try { await work() }
    catch (err) { setError(err instanceof Error ? err.message : 'Unable to complete reporting action') }
    finally { setPending(false) }
  }
  const students = roster.data?.items.filter(r => r.role === 'student') || []
  const orphaned = records.data?.marks.filter(mark => !students.some(student => student.user_id === mark.user_id)) || []
  const reload = () => client.invalidateQueries({ queryKey: ['program-records', audience, cls.id] })
  return <Card className="space-y-4">
    <ProgramReportForm cls={cls} />
    {error && <p role="alert">{error}</p>}
    <details><summary>Class reporting marks and instructor-entered teaching hours</summary>
      <p>Marks apply to this whole class/term. Zero teaching hours means no effort has been recorded. Hours are teaching effort, not print duration.</p>
      {records.isLoading && <p>Loading reporting records…</p>}
      {(records.isError || roster.isError) && <p role="alert">Unable to load class reporting records. <button onClick={() => { records.refetch(); roster.refetch() }}>Retry</button></p>}
      {records.data && roster.data?.items.filter(r => r.role === 'student').map(student => <MarkEditor key={`${student.user_id}:${records.data.marks.find(m => m.user_id === student.user_id)?.revision ?? 0}`}
        row={records.data!.marks.find(m => m.user_id === student.user_id) || { user_id: student.user_id, participant: false, concentrator: false, revision: 0 }}
        name={student.username} pending={pending} save={row => action(async () => { await api.mark(cls.id, row); await reload() })} />)}
      {orphaned.map(row => <MarkEditor key={`${row.user_id}:${row.revision}`} row={row} name={`Former student #${row.user_id} (no active grant)`} pending={pending} save={value => action(async () => { await api.mark(cls.id, value); await reload() })} />)}
      {records.data?.can_approve_hours && <HoursEditor classId={cls.id} audience={audience} pending={pending} instructors={roster.data?.items.filter(r => r.role === 'manager') || []}
        rows={records.data.hours} save={row => action(async () => { await api.hours(cls.id, row); await reload() })} />}
      {!records.data?.can_approve_hours && <p>A tenant administrator records and corrects instructor-entered teaching hours.</p>}
      <ul>{records.data?.hours.map(h => <li key={h.id}>{h.activity_date} · instructor #{h.instructor_id} · {h.hours} teaching hours</li>)}</ul>
      <p>The most recent 100 hour records are shown. To correct any date, select its instructor and date; reload records before retrying a conflict.</p>
    </details>
  </Card>
}
function MarkEditor({ row, name, save, pending }: { row: ProgramMark; name: string; save: (r: ProgramMark) => void; pending: boolean }) {
  const [participant, setParticipant] = useState(row.participant)
  const [concentrator, setConcentrator] = useState(row.concentrator)
  return <form className="flex flex-wrap gap-3 my-3" onSubmit={e => { e.preventDefault(); save({ ...row, participant, concentrator }) }}>
    <span>{name}</span><label><input type="checkbox" checked={participant} onChange={e => { setParticipant(e.target.checked); if (!e.target.checked) setConcentrator(false) }} /> Participant for {name}</label>
    <label><input type="checkbox" checked={concentrator} onChange={e => { setConcentrator(e.target.checked); if (e.target.checked) setParticipant(true) }} /> Concentrator for {name}</label>
    <Button type="submit" disabled={pending}>Save marks for {name}</Button>
  </form>
}
function HoursEditor({ classId, audience, rows, instructors, save, pending }: { classId: string; audience: string; rows: InstructorHours[]; instructors: { user_id: number; username: string }[]; save: (r: InstructorHours) => void; pending: boolean }) {
  const [instructor, setInstructor] = useState('')
  const [day, setDay] = useState(today())
  const [hours, setHours] = useState('')
  const exact = useQuery({ queryKey: ['program-records', audience, classId, day], queryFn: () => api.records(classId, day), enabled: !!day })
  const existing = (exact.data?.hours || rows).find(r => r.instructor_id === Number(instructor) && r.activity_date === day)
  return <form className="grid gap-3 sm:grid-cols-2 my-4" onSubmit={e => { e.preventDefault(); save({ instructor_id: Number(instructor), activity_date: day, hours: Number(hours), revision: existing?.revision ?? 0 }) }}>
    <Select label="Instructor for teaching hours" required value={instructor} onChange={e => setInstructor(e.target.value)}><option value="">Choose instructor</option>{instructors.map(i => <option key={i.user_id} value={i.user_id}>{i.username}</option>)}</Select>
    <Input label="Instruction date" type="date" required value={day} onChange={e => setDay(e.target.value)} />
    <Input label="Instructor-entered teaching hours" type="number" min="0" max="24" step="0.25" required value={hours} onChange={e => setHours(e.target.value)} />
    {existing && <p>Current: {existing.hours} hours. Saving replaces this value; use zero to correct an erroneous entry.</p>}
    <Button type="submit" disabled={pending || !instructor || exact.isLoading || exact.isError}>Record teaching hours</Button>
    {exact.isError && <p role="alert">Unable to load the selected date. <button type="button" onClick={() => exact.refetch()}>Retry</button></p>}
  </form>
}
