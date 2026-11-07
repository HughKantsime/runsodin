import ControlledAudience from './ControlledAudience'
import ControlledRetention from './ControlledRetention'
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useLicense } from '../../LicenseContext'
import { canDo, getCurrentUser } from '../../permissions'
import { controlledParts, type ControlledMark } from '../../api/controlledParts'

export default function ControlledPart({ kind, id }: { kind: 'files' | 'jobs'; id: number }) {
  const license = useLicense()
  return license.hasFeature('controlled_parts') ? <ControlledPartEditor kind={kind} id={id} /> : null
}

function ControlledPartEditor({ kind, id }: { kind: 'files' | 'jobs'; id: number }) {
  const user = getCurrentUser()
  const enabled = true
  const editable = ['admin', 'operator'].includes(user?.role ?? '') && canDo(kind === 'files' ? 'models.edit' : 'jobs.edit')
  const client = useQueryClient()
  const [form, setForm] = useState<ControlledMark>({ controlled: true, label: 'CUI', audience_id: 0, printer_tag: '', retention_days: 30, reason: '' })
  const [editing, setEditing] = useState(false)
  const query = useQuery({ queryKey: ['controlled-part', kind, id], queryFn: () => controlledParts.get(kind, id), enabled })
  const groups = useQuery({ queryKey: ['controlled-part-groups'], queryFn: controlledParts.groups, enabled: enabled && editable && editing })
  const save = useMutation({ mutationFn: () => controlledParts.set(kind, id, form), onSuccess: () => {
    setEditing(false)
    client.invalidateQueries({ queryKey: ['controlled-part'] })
    client.invalidateQueries({ queryKey: ['jobs'] })
    client.invalidateQueries({ queryKey: ['print-files'] })
  } })
  if (!enabled) return null
  return <section aria-label="Controlled part" className="rounded-md border border-[var(--brand-card-border)] p-4 space-y-3">
    <h3 className="font-medium">Controlled part</h3>
    {query.isLoading && <p>Loading marking…</p>}
    {query.isError && <p role="alert">{query.error.message}</p>}
    {query.data && <p>{query.data.controlled ? query.data.policies.map(p => `${p.label} · ${p.printer_tag} printers · timelapses retained ${p.retention_days} days`).join('; ') : 'Unmarked'}</p>}
    {query.data && editable && !editing && <button type="button" onClick={() => {
      const policy = query.data.policies[0]
      setForm({ controlled: true, label: 'CUI', audience_id: groups.data?.[0]?.id ?? 0, printer_tag: '', retention_days: 30, ...policy, reason: '' })
      save.reset(); setEditing(true)
    }}>Change marking</button>}
    {editing && <form className="grid gap-3" onSubmit={event => { event.preventDefault(); save.mutate() }}>
      <label>Controlled <input type="checkbox" checked={form.controlled} onChange={e => setForm({ ...form, controlled: e.target.checked })} /></label>
      <label>Label (CUI, ITAR, or custom) <input required maxLength={80} value={form.label} onChange={e => setForm({ ...form, label: e.target.value })} /></label>
      <label>Controlled audience <select required value={form.audience_id || ''} onChange={e => setForm({ ...form, audience_id: Number(e.target.value) })}>
        <option value="">Select audience</option>{groups.data?.map(g => <option key={g.id} value={g.id}>{g.name}</option>)}
      </select></label>
      {groups.isError && <p role="alert">{groups.error.message}</p>}
      <label>Allowed printer group tag <input required maxLength={100} value={form.printer_tag} onChange={e => setForm({ ...form, printer_tag: e.target.value })} /></label>
      <label>Timelapse retention (days) <input required type="number" min={1} max={3650} value={form.retention_days} onChange={e => setForm({ ...form, retention_days: Number(e.target.value) })} /></label>
      <label>Reason <textarea required maxLength={500} value={form.reason} onChange={e => setForm({ ...form, reason: e.target.value })} /></label>
      <p className="text-sm">You must belong to the controlled audience. Linked file and job restrictions also apply. Active work cannot be reclassified.</p>
      <button disabled={save.isPending || !form.reason.trim() || !form.audience_id}>Save marking</button>
      <button type="button" disabled={save.isPending} onClick={() => setEditing(false)}>Cancel</button>
    </form>}
    {editable && <ControlledRetention />}
    {user?.role === 'admin' && user.group_id == null && <ControlledAudience />}
    {save.isError && <p role="alert">{save.error.message}</p>}
    {save.isSuccess && <p role="status">Marking saved</p>}
  </section>
}
