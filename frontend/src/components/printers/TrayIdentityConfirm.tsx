import { useState } from 'react'
import { printers, spools as spoolApi } from '../../api'
import { canDo } from '../../permissions'

type Props = {
  printerId: number
  slot: number
  observedState: string
  boundSpool: number | null
  spools: { id: number; filament_brand: string | null; filament_name: string | null }[]
  filaments: { id: string | number; display_name: string; source: string }[]
  onConfirmed: () => void
}

export default function TrayIdentityConfirm({ printerId, slot, observedState, boundSpool, spools, filaments, onConfirmed }: Props) {
  const [choosing, setChoosing] = useState(false)
  const [selected, setSelected] = useState('')
  const [library, setLibrary] = useState('')
  const [weight, setWeight] = useState('1000')
  const [created, setCreated] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirmed, setConfirmed] = useState<string | null>(null)
  if (confirmed === observedState) return null

  const confirm = async (decision: 'same' | 'new') => {
    setBusy(true)
    setError('')
    try {
      let spoolId = selected ? Number(selected) : created
      if (decision === 'new' && !spoolId && library) {
        const spool = await spoolApi.create({ filament_id: Number(library.replace(/^lib_/, '')), initial_weight_g: Number(weight), remaining_weight_g: Number(weight) })
        spoolId = spool.id
        setCreated(spoolId)
      }
      if (decision === 'new' && !spoolId) throw new Error('Select or create a spool')
      await printers.confirmTrayIdentity(printerId, slot, { observed_state: observedState, decision, ...(decision === 'new' ? { spool_id: spoolId! } : {}) })
      setConfirmed(observedState)
      onConfirmed()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not confirm spool')
    } finally {
      setBusy(false)
    }
  }

  return <div className="col-span-full min-w-0 [overflow-wrap:anywhere] rounded-md border border-amber-500/40 p-2 text-sm" onClick={e => e.stopPropagation()} onPointerDown={e => e.stopPropagation()}>
    <p>Slot {slot}: spool may have changed?</p>
    <p className="text-xs text-[var(--brand-text-muted)]">Remaining unknown. Consumption is held until confirmation. Printing can continue.</p>
    {canDo('printers.slots') && <>
      <div className="flex gap-3 mt-2">
        <button disabled={busy || boundSpool == null} onClick={() => confirm('same')}>Same spool</button>
        <button disabled={busy} onClick={() => setChoosing(true)}>New spool</button>
      </div>
      {choosing && <div className="space-y-2 mt-2">
        <select aria-label="New spool" value={selected} disabled={busy || created != null} onChange={e => setSelected(e.target.value)} className="w-full bg-[var(--brand-input-bg)]">
          <option value="">Select a spool</option>
          {spools.filter(s => s.id !== boundSpool).map(s => <option key={s.id} value={s.id}>#{s.id} {s.filament_brand} {s.filament_name}</option>)}
        </select>
        {!selected && canDo('spools.edit') && <>
          <label className="block">Or create a spool from its actual filament record
            <select aria-label="Filament for new spool" value={library} disabled={busy || created != null} onChange={e => setLibrary(e.target.value)} className="w-full bg-[var(--brand-input-bg)]">
              <option value="">Select filament</option>
              {filaments.filter(f => f.source === 'library').map(f => <option key={f.id} value={f.id}>{f.display_name}</option>)}
            </select>
          </label>
          <label>Starting grams <input aria-label="Starting grams" type="number" min="1" value={weight} disabled={busy || created != null} onChange={e => setWeight(e.target.value)} className="bg-[var(--brand-input-bg)] w-24" /></label>
        </>}
        <button disabled={busy || (!selected && !created && (!library || Number(weight) <= 0))} onClick={() => confirm('new')}>{busy ? 'Saving…' : 'Confirm new spool'}</button>
      </div>}
    </>}
    {error && <p role="alert" className="text-red-400">{error}</p>}
  </div>
}
