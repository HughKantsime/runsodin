import { useState } from 'react'
import { Button, Input, Select } from '../ui'
import type { TrainingDefinition, TrainingPrinter } from '../../api/educationTraining'

const empty: TrainingDefinition = { name: '', description: '', scope_kind: 'printer_group', scope_value: '', quiz: [], pass_mark: 80, instructor_required: true, expiry_days: null, active: true }
export default function TrainingEditor({ initial, pending, onSave, onCancel, printers = [], schoolPrinters = [], canManageSchoolWide = false }: {
  canManageSchoolWide?: boolean; printers?: TrainingPrinter[]; schoolPrinters?: TrainingPrinter[];
  initial?: TrainingDefinition; pending: boolean; onSave: (value: TrainingDefinition) => void; onCancel: () => void
}) {
  const [form, setForm] = useState<TrainingDefinition>(() => initial ? {
    school_wide: initial.school_wide ?? false, name: initial.name, description: initial.description, scope_kind: initial.scope_kind, scope_value: initial.scope_value,
    quiz: initial.quiz, pass_mark: initial.pass_mark, instructor_required: initial.instructor_required, expiry_days: initial.expiry_days, active: initial.active,
  } : empty)
  const update = (patch: Partial<TrainingDefinition>) => setForm({ ...form, ...patch })
  const question = (index: number, patch: Partial<TrainingDefinition['quiz'][number]>) => update({ quiz: form.quiz.map((q, i) => i === index ? { ...q, ...patch } : q) })
  const requirementChanged = initial && (['scope_kind', 'scope_value', 'pass_mark', 'instructor_required'] as const).some(key => form[key] !== initial[key]) ||
    !!initial && (JSON.stringify(form.quiz) !== JSON.stringify(initial.quiz) || !!form.school_wide !== !!initial.school_wide)
  const matched = (form.school_wide ? schoolPrinters : printers).filter(p => form.scope_kind === 'printer_group' ||
    (p[form.scope_kind] ?? '').trim().toLowerCase() === form.scope_value.trim().toLowerCase())
  return <form className="space-y-3" onSubmit={event => { event.preventDefault(); onSave(form) }}>
    <label className="block">Training name<Input required maxLength={160} value={form.name} onChange={e => update({ name: e.target.value })} /></label>
    <label className="block">Description<textarea className="w-full bg-[var(--brand-card-bg)] border rounded p-2" maxLength={4000} value={form.description} onChange={e => update({ description: e.target.value })} /></label>
    {canManageSchoolWide && <label className="block"><input type="checkbox" checked={!!form.school_wide} onChange={e => update({ school_wide: e.target.checked })} /> School-wide training</label>}
    <label className="block">Printer scope<Select value={form.scope_kind} onChange={e => update({ scope_kind: e.target.value as TrainingDefinition['scope_kind'], scope_value: '' })}>
      <option value="printer_group">{form.school_wide ? 'All school printers' : 'All printers authorized for this class'}</option><option value="machine_type">Machine type</option><option value="model">Printer model</option>
    </Select></label>
    {form.scope_kind !== 'printer_group' && <label className="block">Exact {form.scope_kind === 'machine_type' ? 'machine type (e.g. P1S)' : 'model name'}<Input required maxLength={160} value={form.scope_value} onChange={e => update({ scope_value: e.target.value })} /></label>}
    <p>Matched printers: {matched.map(p => p.name).join(', ') || 'None — choose a scope that matches an authorized printer.'}</p>
    <label className="block"><input type="checkbox" checked={form.instructor_required} onChange={e => update({ instructor_required: e.target.checked })} /> Require instructor sign-off</label>
    <label className="block">Expires after days (blank means no expiry)<Input type="number" min={1} max={3650} value={form.expiry_days ?? ''} onChange={e => update({ expiry_days: e.target.value ? Number(e.target.value) : null })} /></label>
    {form.quiz.map((q, index) => <fieldset className="border rounded p-3 space-y-2" key={index}>
      <legend>Question {index + 1}</legend>
      <label className="block">Question text<Input required maxLength={500} value={q.prompt} onChange={e => question(index, { prompt: e.target.value })} /></label>
      {q.choices.map((choice, ci) => <label className="block" key={ci}>Choice {ci + 1}<Input required maxLength={300} value={choice} onChange={e => question(index, { choices: q.choices.map((old, i) => i === ci ? e.target.value : old) })} /></label>)}
      <label className="block">Correct choice<Select value={q.correct ?? 0} onChange={e => question(index, { correct: Number(e.target.value) })}>{q.choices.map((_, ci) => <option key={ci} value={ci}>{ci + 1}</option>)}</Select></label>
      <Button type="button" variant="secondary" disabled={q.choices.length >= 8} onClick={() => question(index, { choices: [...q.choices, ''] })}>Add choice</Button>
      <Button type="button" variant="secondary" onClick={() => update({ quiz: form.quiz.filter((_, i) => i !== index) })}>Remove question</Button>
    </fieldset>)}
    <Button type="button" variant="secondary" disabled={form.quiz.length >= 30} onClick={() => update({ quiz: [...form.quiz, { prompt: '', choices: ['', ''], correct: 0 }] })}>Add quiz question</Button>
    {form.quiz.length > 0 && <label className="block">Pass mark (%)<Input type="number" min={1} max={100} required value={form.pass_mark} onChange={e => update({ pass_mark: Number(e.target.value) })} /></label>}
    {requirementChanged && <p role="alert">Saving these requirement changes invalidates existing completions. Students must complete the training again.</p>}
    {!form.instructor_required && !form.quiz.length && <p role="alert">Add a quiz or require instructor sign-off.</p>}
    <div className="flex gap-2"><Button type="submit" disabled={pending || !matched.length || (!form.instructor_required && !form.quiz.length)}>Save training</Button><Button type="button" variant="secondary" onClick={onCancel}>Cancel</Button></div>
  </form>
}
