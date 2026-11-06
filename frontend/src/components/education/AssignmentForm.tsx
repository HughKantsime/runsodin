import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { education } from '../../api'
import { educationClasses, type AssignmentRow, type AssignmentBody } from '../../api/educationClasses'
import { Button, Input, Select } from '../ui'

export default function AssignmentForm({ classId, centerId, audience, existing, onSaved, onCancel }: {
  classId: string; centerId: number; audience: string; existing?: AssignmentRow; onSaved: () => void; onCancel: () => void
}) {
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [auto, setAuto] = useState(!!existing?.auto_printer_ids.length)
  const printers = useQuery({ queryKey: ['class-printers', audience, centerId], queryFn: async () => {
    const items = []; let cursor: string | undefined
    do { const page = await education.listCenterPrinters(centerId, cursor); items.push(...page.items); cursor = page.next_cursor || undefined } while (cursor)
    return items.filter(p => p.state === 'active')
  } })
  return <form className="space-y-3 rounded border border-[var(--brand-card-border)] p-4" onSubmit={async e => {
    e.preventDefault(); if (pending) return
    const form = new FormData(e.currentTarget)
    const num = (key: string) => String(form.get(key) || '').trim() ? Number(form.get(key)) : null
    const minutes = (key: string) => num(key) === null ? null : Math.round(num(key)! * 60)
    const due = String(form.get('due') || '')
    const data: AssignmentBody = {
      title: String(form.get('title')), brief: String(form.get('brief') || ''), due_at: due ? new Date(due).toISOString() : null,
      max_print_seconds: minutes('minutes'), max_material_grams: num('grams'), submission_quota: num('quota'),
      allowed_materials: String(form.get('materials') || '').split(',').map(x => x.trim()).filter(Boolean),
      auto_print_seconds: auto ? minutes('autoMinutes') : null, auto_material_grams: auto ? num('autoGrams') : null,
      auto_printer_ids: auto ? form.getAll('printer').map(Number) : [], state: String(form.get('state') || 'active') as 'active' | 'closed',
    }
    setPending(true); setError('')
    try { await educationClasses.save(classId, data, existing); onSaved() }
    catch (err) { setError(err instanceof Error ? err.message : 'Unable to save assignment') }
    finally { setPending(false) }
  }}>
    <h3 className="font-semibold">{existing ? 'Edit assignment' : 'New assignment'}</h3>
    <Input name="title" label="Assignment title" required maxLength={200} defaultValue={existing?.title} />
    <label className="block">Brief<textarea name="brief" className="block w-full rounded bg-[var(--brand-input-bg)] p-2" maxLength={10000} defaultValue={existing?.brief} /></label>
    <Input name="due" label="Due date (your local time)" type="datetime-local" defaultValue={existing?.due_at ? localDate(existing.due_at) : ''} />
    <div className="grid gap-3 sm:grid-cols-3">
      <Input name="minutes" label="Maximum minutes per print" type="number" min="0.01" step="any" defaultValue={existing?.max_print_seconds ? existing.max_print_seconds / 60 : ''} />
      <Input name="grams" label="Maximum grams per print" type="number" min="0.01" step="any" defaultValue={existing?.max_material_grams ?? ''} />
      <Input name="quota" label="Submissions per student" type="number" min="1" defaultValue={existing?.submission_quota ?? ''} />
    </div>
    <Input name="materials" label="Allowed materials (comma separated; blank allows any)" defaultValue={existing?.allowed_materials.join(', ')} />
    <p className="text-sm">Only submissions that reached approval count toward the quota, including those that later fail. Leave limits blank for no assignment limit.</p>
    <Select name="state" label="Assignment availability" defaultValue={existing?.state || 'active'}><option value="active">Open</option><option value="closed">Closed</option></Select>
    <label className="flex gap-2"><input type="checkbox" checked={auto} onChange={e => setAuto(e.target.checked)} />Auto-approve eligible submissions</label>
    {auto && <fieldset className="space-y-3"><legend>Auto-approval rules</legend>
      <Input name="autoMinutes" label="Auto-approve at most minutes" type="number" min="0.01" step="any" required defaultValue={existing?.auto_print_seconds ? existing.auto_print_seconds / 60 : ''} />
      <Input name="autoGrams" label="Auto-approve at most grams" type="number" min="0.01" step="any" required defaultValue={existing?.auto_material_grams ?? ''} />
      <p>Approved printers (the first selected printer by ID is checked; otherwise work stays in manual review):</p>
      {printers.isLoading && <p>Loading printers…</p>}
      {printers.isError && <p role="alert">Unable to load authorized printers.</p>}
      {printers.data?.map(p => <label className="flex gap-2" key={p.printer_id}><input name="printer" type="checkbox" value={p.printer_id} defaultChecked={existing?.auto_printer_ids.includes(p.printer_id)} />{p.name}</label>)}
      {printers.data?.length === 0 && <p>No authorized printers. Ask an administrator to update the cost center.</p>}
      <p className="text-sm">Compatibility and current instructor access are rechecked. No rule bypasses dispatch safety checks.</p>
    </fieldset>}
    {error && <p role="alert">{error} Reload the assignment before retrying a revision conflict.</p>}
    <Button type="submit" disabled={pending || (auto && !printers.data?.length)}>{pending ? 'Saving…' : 'Save assignment'}</Button>
    <Button type="button" variant="secondary" onClick={onCancel}>Cancel</Button>
  </form>
}
function localDate(value: string) { const date = new Date(value); return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16) }
