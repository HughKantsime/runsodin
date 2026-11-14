import { useId, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { fetchAPI } from '../../api/client'
import { canDo, getCurrentUser } from '../../permissions'
import Modal from '../ui/Modal'

import type { FailureCorrection } from '../../types/job'

type Props = {
  printId: number; status: string; endedAt?: string | null; grams?: number | null
  correction?: FailureCorrection | null; onCorrected?: () => void; inline?: boolean
  canCorrect?: boolean
}
const reasons = [
  ['no_filament', 'No filament fed / printed air'],
  ['filament_supply', 'Filament ran out or tangled'],
  ['first_layer', 'First layer did not stick'],
  ['came_loose', 'Print came loose or spaghetti'],
  ['under_extrusion', 'Nozzle clog or under-extrusion'],
  ['printer_fault', 'Printer fault or error code'],
  ['power_connection', 'Power or connection loss'],
  ['other', 'Other'],
]

function canCorrectPrint() {
  const user = getCurrentUser()
  return !!user && (user.role === 'admin' && !user.group_id ||
    ['operator', 'admin'].includes(user.role) && canDo('jobs.edit'))
}

export default function PrintFailureControl(props: Props) {
  if (!props.printId || (!props.correction && (props.status !== 'cancelled' || !props.endedAt))) return null
  return <Control {...props} />
}

function Control({ printId, grams, correction, onCorrected, inline = false, canCorrect = false }: Props) {
  const client = useQueryClient()
  const id = useId()
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [note, setNote] = useState('')
  const [amount, setAmount] = useState('')
  const [zero, setZero] = useState(false)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [warnings, setWarnings] = useState<string[]>([])
  const editable = canCorrect && canCorrectPrint()
  const finish = async () => {
    await client.invalidateQueries()
    setOpen(false); setWarnings([])
    onCorrected?.()
  }
  const save = async (undo: boolean) => {
    if (pending || !canCorrect || !canCorrectPrint()) return
    if (!undo && !zero && amount.trim() !== '') {
      if (!Number.isFinite(Number(amount)) || Number(amount) < 0) {
        setError('Enter a finite number of grams, zero or greater.'); return
      }
      if (grams != null && Number(amount) > grams) {
        setError('Corrected material use cannot exceed the recorded amount.'); return
      }
    }
    setPending(true); setError('')
    try {
      const result = await fetchAPI<{ warnings?: string[] }>(`/print-jobs/${printId}/${undo ? 'undo-failure' : 'mark-failed'}`, {
        method: 'POST', ...(undo ? {} : { body: JSON.stringify({ reason, note,
          ...(amount.trim() !== '' && !zero ? { filament_used_g: Number(amount) } : {}), no_material_used: zero }) }),
      })
      if (result?.warnings?.length) setWarnings(result.warnings)
      else await finish()
    } catch (failure: any) {
      setError(failure.message || 'The print could not be updated. Try again.')
    } finally { setPending(false) }
  }
  const title = correction ? 'Back to cancelled' : 'Mark as failed'
  const form = warnings.length ? (
    <div className="space-y-4">
      <div role="status"><p>The print is back to cancelled.</p>{warnings.map(warning => <p key={warning}>{warning}</p>)}</div>
      <button type="button" className="min-h-11 px-3" onClick={() => void finish()}>Done</button>
    </div>
  ) : correction ? (
    <div className="space-y-4">
      <p>Restore the cancelled outcome and original material use where possible? Later accounting and job workflow changes will be kept and explained.</p>
      {error && <p role="alert">{error}</p>}
      <div data-modal-footer className="flex flex-wrap gap-3">
        <button type="button" disabled={pending} onClick={() => void save(true)} className="min-h-11 px-3">Confirm back to cancelled</button>
        <button type="button" disabled={pending} onClick={() => setOpen(false)} className="min-h-11 px-3">Keep correction</button>
      </div>
    </div>
  ) : (
    <form className="space-y-4" onSubmit={event => { event.preventDefault(); void save(false) }}>
        <div><label htmlFor={`${id}-reason`}>Reason</label>
          <select id={`${id}-reason`} required value={reason} onChange={event => setReason(event.target.value)} className="block w-full min-h-11 bg-[var(--brand-input-bg)] rounded border border-[var(--brand-card-border)]">
            <option value="">Choose a reason</option>
            {reasons.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select></div>
        <div><label htmlFor={`${id}-note`}>Note (optional)</label>
          <textarea id={`${id}-note`} maxLength={1000} value={note} onChange={event => setNote(event.target.value)} className="block w-full bg-[var(--brand-input-bg)] rounded border border-[var(--brand-card-border)]" /></div>
        <div><label htmlFor={`${id}-grams`}>Corrected material use (g, optional)</label>
          <input id={`${id}-grams`} type="text" inputMode="decimal" disabled={zero} value={zero ? '0' : amount} onChange={event => setAmount(event.target.value)} className="block w-full min-h-11 bg-[var(--brand-input-bg)] rounded border border-[var(--brand-card-border)]" />
          <p className="text-xs">Leave blank to keep recorded use. Changes that cannot be undone exactly will be refused.</p></div>
        <label className="flex items-center gap-2 min-h-11"><input type="checkbox" checked={zero} onChange={event => setZero(event.target.checked)} />No material was used</label>
        {error && <p role="alert">{error}</p>}
        <div data-modal-footer className="flex flex-wrap gap-3">
          <button type="submit" disabled={pending || !reason} className="min-h-11 px-3 rounded bg-[var(--brand-primary)] text-white">{pending ? 'Saving…' : 'Save failure correction'}</button>
          <button type="button" disabled={pending} onClick={() => setOpen(false)} className="min-h-11 px-3">Cancel</button>
        </div>
    </form>
  )
  return <div className="text-sm font-normal break-words" onClick={event => event.stopPropagation()}>
    {correction && <div>
      <p>Marked as failed by {correction.by} on {new Date(correction.at).toLocaleString()}: {correction.reason_label}</p>
      {correction.note && <p className="whitespace-pre-wrap">{correction.note}</p>}
      <p>Original material: {correction.original_grams == null ? 'Unknown' : `${correction.original_grams} g`}. Corrected material: {correction.corrected_grams == null ? 'Unknown' : `${correction.corrected_grams} g`}.</p>
    </div>}
    {editable && <button type="button" className="min-h-11 underline text-[var(--brand-primary)]" disabled={pending}
      onClick={() => setOpen(true)}>{pending ? 'Saving…' : correction ? 'Back to cancelled' : 'Mark as failed'}</button>}
    {error && !open && <p role="alert">{error}</p>}
    {inline ? open && <section aria-label={title}>{form}</section> : (
      <Modal isOpen={open} onClose={() => { if (!pending) { if (warnings.length) void finish(); else setOpen(false) } }} title={title} size="md">
        {form}
      </Modal>
    )}
  </div>
}
