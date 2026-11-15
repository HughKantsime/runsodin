import { useId, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { fetchAPI } from '../../api/client'
import { canDo, getCurrentUser } from '../../permissions'
import Modal from '../ui/Modal'
import { Button, Select, Textarea } from '../ui'
import ExpandableNote from '../ui/ExpandableNote'
import { formatDate } from '../../utils/shared'

import type { FailureCorrection } from '../../types/job'

type Props = {
  printId: number; status: string; endedAt?: string | null
  correction?: FailureCorrection | null; onCorrected?: () => void; inline?: boolean
  canCorrect?: boolean; compactNote?: boolean
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

function Control({ printId, correction, onCorrected, inline = false, canCorrect = false, compactNote = false }: Props) {
  const client = useQueryClient()
  const id = useId()
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [note, setNote] = useState('')
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
    setPending(true); setError('')
    try {
      const result = await fetchAPI<{ warnings?: string[] }>(`/print-jobs/${printId}/${undo ? 'undo-failure' : 'mark-failed'}`, {
        method: 'POST', ...(undo ? {} : { body: JSON.stringify({ reason, note }) }),
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
      <div role="status"><p>Undo finished. See the preserved records below.</p>{warnings.map(warning => <p key={warning}>{warning}</p>)}</div>
      <Button type="button" className="min-h-11" onClick={() => void finish()}>Done</Button>
    </div>
  ) : correction ? (
    <div className="space-y-4">
      <p>Restore the cancelled outcome where it still belongs to this correction? Later outcomes and new archives will be kept and explained.</p>
      {error && <p role="alert">{error}</p>}
      <div data-modal-footer className="flex flex-wrap gap-3">
        <Button type="button" disabled={pending} onClick={() => void save(true)} className="min-h-11">Confirm back to cancelled</Button>
        <Button type="button" variant="secondary" disabled={pending} onClick={() => setOpen(false)} className="min-h-11">Keep correction</Button>
      </div>
    </div>
  ) : (
    <form className="space-y-4" onSubmit={event => { event.preventDefault(); void save(false) }}>
        <Select id={`${id}-reason`} label="Reason" required value={reason} onChange={event => setReason(event.target.value)} className="min-h-11">
          <option value="">Choose a reason</option>
          {reasons.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </Select>
        <Textarea id={`${id}-note`} label="Note (optional)" maxLength={1000} value={note} onChange={event => setNote(event.target.value)} className="min-h-11" />
        <p>This changes the outcome only. Recorded filament and cost stay as they are.</p>
        {error && <p role="alert">{error}</p>}
        <div data-modal-footer className="flex flex-wrap gap-3">
          <Button type="submit" disabled={pending || !reason} className="min-h-11">{pending ? 'Saving…' : 'Save failure correction'}</Button>
          <Button type="button" variant="secondary" disabled={pending} onClick={() => setOpen(false)} className="min-h-11">Cancel</Button>
        </div>
    </form>
  )
  return <div className="text-sm font-normal break-words" onClick={event => event.stopPropagation()}>
    {correction && <div>
      <p>Marked as failed by {correction.by} on {formatDate(correction.at)}: {correction.reason_label}</p>
      {correction.note && (compactNote ? <ExpandableNote text={correction.note} /> : <p className="whitespace-pre-wrap">{correction.note}</p>)}
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
