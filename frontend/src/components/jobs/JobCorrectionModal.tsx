import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Modal } from '../ui'

const identity = (user: any) => user ? JSON.stringify([user.id, user.role, user.group_id]) : ''

export default function JobCorrectionModal({ job, actor, onClose, onSubmit }: {
  job: any; actor: any; onClose: () => void;
  onSubmit: (id: number, details: any) => Promise<any>
}) {
  const client = useQueryClient()
  const owner = identity(actor)
  const [name, setName] = useState(job.item_name || '')
  const [quantity, setQuantity] = useState(String(job.quantity ?? ''))
  const [notes, setNotes] = useState(job.notes || '')
  const [date, setDate] = useState(job.due_date?.split('T')[0] || '')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const live = useRef(true)
  const sending = useRef(false)
  useEffect(() => {
    live.current = true
    const unsubscribe = client.getQueryCache().subscribe(event => {
      if (JSON.stringify(event.query.queryKey) === JSON.stringify(['auth', 'me']) &&
          identity(event.query.state.data) !== owner) {
        live.current = false; onClose()
      }
    })
    return () => { live.current = false; unsubscribe() }
  }, [client, owner, onClose])
  const current = () => live.current && identity(client.getQueryData(['auth', 'me'])) === owner &&
    Number.isInteger(actor?.id) && actor.id > 0 && Number.isInteger(actor?.group_id) && actor.group_id > 0 &&
    actor.group_id === job.charged_to_org_id && actor?.role === 'viewer' && actor?.id === job.submitted_by && job.status === 'rejected'
  async function save(event: FormEvent) {
    event.preventDefault()
    if (sending.current) return
    if (!current()) { onClose(); return }
    sending.current = true; setPending(true); setError('')
    try {
      await onSubmit(job.id, { item_name: name.trim(), quantity: Number(quantity), notes: notes || null,
        due_date: date ? `${date}T00:00:00Z` : null })
      if (current()) { void client.invalidateQueries({ queryKey: ['jobs'] }); onClose() }
    } catch (failure: any) {
      if (current()) setError(failure?.message || 'Correction could not be confirmed. Reload before trying again.')
    } finally {
      sending.current = false
      if (current()) setPending(false)
    }
  }
  if (!current()) return null
  return <Modal isOpen onClose={() => { if (!pending) onClose() }} title="Correct rejected submission" size="md">
    <form onSubmit={save} className="space-y-4">
      <p>The job stays rejected. Resubmit it separately when your corrections are ready.</p>
      <label className="block">Item name<input className="block w-full mt-1 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm" aria-label="Item name" required maxLength={200} value={name} onChange={event => setName(event.target.value)} disabled={pending} /></label>
      <label className="block">Quantity<input className="block w-full mt-1 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm" aria-label="Quantity" type="number" min={1} max={10000} required value={quantity} onChange={event => setQuantity(event.target.value)} disabled={pending} /></label>
      <label className="block">Due date<input className="block w-full mt-1 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm" aria-label="Due date" type="date" value={date} onChange={event => setDate(event.target.value)} disabled={pending} /></label>
      <label className="block">Notes<textarea className="block w-full mt-1 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm" aria-label="Notes" maxLength={10000} value={notes} onChange={event => setNotes(event.target.value)} disabled={pending} /></label>
      {error && <p role="alert">{error}</p>}
      <button className="px-4 py-2 rounded-md bg-[var(--brand-input-bg)]" type="button" disabled={pending} onClick={onClose}>Cancel</button>
      <button className="ml-3 px-4 py-2 rounded-md bg-[var(--brand-primary)] disabled:opacity-50" type="submit" disabled={pending || !name.trim() || !Number.isInteger(Number(quantity)) || Number(quantity) < 1 || Number(quantity) > 10000}>{pending ? 'Saving...' : 'Save correction'}</button>
    </form>
  </Modal>
}
