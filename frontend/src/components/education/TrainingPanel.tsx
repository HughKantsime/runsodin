import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, Card, Select } from '../ui'
import type { EducationCostCenter } from '../../types'
import { educationTraining, type Training, type TrainingDefinition } from '../../api/educationTraining'
import TrainingEditor from './TrainingEditor'

const label = (value: string) => value.replace(/_/g, ' ')
export default function TrainingPanel({ centers, audience }: { centers: EducationCostCenter[]; audience: string }) {
  const [centerId, setCenterId] = useState(centers[0]?.id ?? 0)
  const center = centers.some(c => c.id === centerId) ? centerId : centers[0]?.id ?? 0
  if (!centers.length) return <Card>No classes are available for training.</Card>
  return <div className="space-y-4">
    <label className="block">Training class<Select value={center} onChange={e => setCenterId(Number(e.target.value))}>{centers.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</Select></label>
    <CenterTraining key={`${audience}-${center}`} center={center} audience={audience} />
  </div>
}

function CenterTraining({ center, audience }: { center: number; audience: string }) {
  const qc = useQueryClient()
  const [editing, setEditing] = useState<Training | 'new' | null>(null)
  const query = useQuery({ queryKey: ['education-training', audience, center], queryFn: () => educationTraining.list(center) })
  const save = useMutation({ mutationFn: (data: TrainingDefinition) => editing && editing !== 'new' ? educationTraining.update(editing, data) : educationTraining.create(center, data),
    onSuccess: () => { setEditing(null); qc.invalidateQueries({ queryKey: ['education-training', audience] }) } })
  if (query.isLoading) return <p>Loading training…</p>
  if (query.isError) return <p role="alert">Unable to load training: {query.error.message}</p>
  return <>
    <p>Complete all required training for your printer before submitting. A quiz and instructor sign-off are both required when configured. Teachers and school admins bypass their own training requirements.</p>
    {query.data?.can_manage && <Button onClick={() => { save.reset(); setEditing('new') }}>New training</Button>}
    {save.isError && <p role="alert">{save.error.message}</p>}
    {editing && <Card><TrainingEditor key={editing === 'new' ? 'new' : editing.id} initial={editing === 'new' ? undefined : editing} printers={query.data?.printers} schoolPrinters={query.data?.school_printers} canManageSchoolWide={query.data?.can_manage_school_wide} pending={save.isPending} onSave={data => save.mutate(data)} onCancel={() => setEditing(null)} /></Card>}
    {!query.data?.items.length && <Card>No training requirements have been configured for this class.</Card>}
    {query.data?.items.map(training => <TrainingCard key={`${training.id}-${training.revision}`} training={training} audience={audience} onEdit={() => { save.reset(); setEditing(training) }} />)}
  </>
}

function TrainingCard({ training, audience, onEdit }: { training: Training; audience: string; onEdit: () => void }) {
  const qc = useQueryClient()
  const [answers, setAnswers] = useState<number[]>(training.quiz.map(() => -1))
  const [showRoster, setShowRoster] = useState(false)
  const [message, setMessage] = useState('')
  const refresh = () => { qc.invalidateQueries({ queryKey: ['education-training', audience] }); qc.invalidateQueries({ queryKey: ['training-roster', audience, training.id] }) }
  const quiz = useMutation({ mutationFn: () => educationTraining.quiz(training, answers), onSuccess: result => { setMessage(`Score: ${result.quiz_score}%. Status: ${label(result.status)}.`); refresh() } })
  const archive = useMutation({ mutationFn: () => educationTraining.update(training, { school_wide: training.school_wide, name: training.name, description: training.description, scope_kind: training.scope_kind, scope_value: training.scope_value, quiz: training.quiz, pass_mark: training.pass_mark, instructor_required: training.instructor_required, expiry_days: training.expiry_days, active: !training.active }), onSuccess: refresh })
  const roster = useQuery({ queryKey: ['training-roster', audience, training.id], queryFn: () => educationTraining.roster(training.id), enabled: showRoster && training.can_manage })
  const assess = useMutation({ mutationFn: ({ user, action }: { user: number; action: 'signoff' | 'revoke' }) => educationTraining.assess(training, user, action), onSuccess: refresh })
  return <Card className="space-y-3">
    <h2 className="font-semibold">{training.name}{!training.active && ' (archived)'}</h2>
    <p className="whitespace-pre-wrap">{training.description}</p>
    <p className="text-sm">Applies to: {training.scope_kind === 'printer_group' ? (training.school_wide ? 'all school printers' : 'class authorized printers') : training.scope_value}. {training.instructor_required && 'Instructor sign-off required. '}{training.quiz.length > 0 && `Quiz pass mark: ${training.pass_mark}%. `}{training.expiry_days ? `Valid for ${training.expiry_days} days.` : 'No expiry.'}</p>
    <p>Matched printers: {training.matched_printers?.map(p => p.name).join(', ') || 'None'}</p>
    {training.active && training.matched_printers?.length === 0 && <p role="alert">This training no longer matches a printer. Review its scope and printer settings.</p>}
    {training.record?.cooldown_until && <p>Quiz cooldown until {new Date(training.record.cooldown_until).toLocaleString()}</p>}
    {!training.can_manage && <p>Status: {label(training.status)}{training.record?.expires_at && ` · Expires ${new Date(training.record.expires_at).toLocaleDateString()}`}</p>}
    {!training.can_manage && training.active && training.quiz.length > 0 && !['complete', 'instructor_required'].includes(training.status) && <form className="space-y-3" onSubmit={e => { e.preventDefault(); quiz.mutate() }}>
      {training.quiz.map((q, index) => <fieldset key={index}><legend>{q.prompt}</legend>{q.choices.map((choice, ci) => <label className="block" key={ci}><input type="radio" required name={`${training.id}-${index}`} value={ci} checked={answers[index] === ci} onChange={() => setAnswers(answers.map((old, i) => i === index ? ci : old))} /> {choice}</label>)}</fieldset>)}
      <Button type="submit" disabled={quiz.isPending || answers.some(a => a < 0)}>Submit quiz</Button>
    </form>}
    {message && <p role="status">{message}</p>}
    {[quiz.error, archive.error, assess.error, roster.error].filter(Boolean).map((error, i) => <p role="alert" key={i}>{error!.message}</p>)}
    {training.can_manage && <div className="flex flex-wrap gap-2">{training.can_edit && <><Button variant="secondary" onClick={onEdit}>Edit training</Button><Button variant="secondary" disabled={archive.isPending} onClick={() => archive.mutate()}>{training.active ? 'Archive requirement' : 'Reactivate requirement'}</Button></>}<Button variant="secondary" onClick={() => setShowRoster(!showRoster)}>{showRoster ? 'Hide students' : 'View student sign-offs'}</Button></div>}
    {showRoster && roster.isLoading && <p>Loading students…</p>}
    {showRoster && roster.data?.items.map(student => <div className="flex flex-wrap items-center gap-3 border-t pt-3" key={student.user_id}>
      <span>{student.username}: {label(student.status)}{!student.active && ' (inactive)'}</span>
      {student.record?.expires_at && <span>Expires {new Date(student.record.expires_at).toLocaleDateString()}</span>}
      {student.record && <span>Quiz attempts: {student.record.quiz_attempts ?? 0}{student.record.cooldown_until && ` · Cooldown until ${new Date(student.record.cooldown_until).toLocaleString()}`}</span>}
      {student.record?.quiz_score != null && <span>Quiz: {student.record.quiz_score}%</span>}
      {training.active && <>
        {student.active && training.instructor_required && student.status !== 'complete' && <Button disabled={assess.isPending} onClick={() => assess.mutate({ user: student.user_id, action: 'signoff' })}>Sign off {student.username}</Button>}
        {student.record && student.status !== 'revoked' && <Button variant="secondary" disabled={assess.isPending} onClick={() => assess.mutate({ user: student.user_id, action: 'revoke' })}>Revoke {student.username}</Button>}
      </>}
    </div>)}
  </Card>
}
