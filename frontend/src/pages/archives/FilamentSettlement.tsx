import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { archives, spools } from '../../api'
import type { Archive, NoDeduction, SettlementPreview } from '../../types'
import { canDo } from '../../permissions'
import { Button } from '../../components/ui'
import { formatDate } from '../../utils/shared'

const reasons: [NoDeduction, string][] = [
  ['nothing_extruded', 'Nothing was extruded'], ['recorded_by_hand', 'Recorded by hand'],
  ['spool_not_tracked', 'Spool is not tracked'], ['balance_already_updated', 'Balance already updated'],
]
const inputClass = 'w-full rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-input-bg)] p-2'

export default function FilamentAccounting({ archive }: { archive: Archive }) {
  const [editing, setEditing] = useState(false)
  const [updated, setUpdated] = useState<Archive | null>(null)
  const shown = updated || archive
  if (editing) return <SettlementForm id={archive.id} onCancel={() => setEditing(false)} onSaved={value => { setUpdated(value); setEditing(false) }} />
  const needsSettlement = shown.accounting_state === 'unresolved'
  const needsAttention = needsSettlement || shown.accounting_state === 'reconciliation_required'
  return <>
    {shown.accounting_settled && <div className="mb-4 text-sm" role="status">
      <p>Filament settled{shown.accounting_settled.by ? ` by ${shown.accounting_settled.by}` : ''} on {formatDate(shown.accounting_settled.at)}:</p>
      {shown.consumption?.map((item, i) => <p key={i}>{item.spool_id != null && item.grams_used != null
        ? `${item.grams_used.toFixed(1)} g from spool ${item.spool_id}` : 'No further deduction'}{item.remaining_unknown ? '. The spool balance is still unknown.' : ''}</p>)}
    </div>}
    {needsAttention && <div className="mb-4 rounded-md border border-amber-500/40 p-3 text-sm" role="status">
      <p>{shown.accounting_cause?.title || 'This print needs its filament settled'}</p>
      <p>{shown.accounting_cause?.next_step || 'Settle this print'}</p>
      {shown.consumption?.filter(item => item.slot_number != null).map((item, i) => <p key={i}>Slot {item.slot_number}: {item.grams_used != null ? `${item.grams_used.toFixed(1)} g` : 'Quantity unknown'}</p>)}
      {needsSettlement && canDo('archives.settle_filament') && <Button className="mt-2" onClick={() => setEditing(true)}>Settle filament</Button>}
    </div>}
  </>
}

function SettlementForm({ id, onCancel, onSaved }: { id: number; onCancel: () => void; onSaved: (archive: Archive) => void }) {
  const queryClient = useQueryClient()
  const [entries, setEntries] = useState<{ spool: string; grams: string }[]>([])
  const [initialized, setInitialized] = useState(false)
  const [reason, setReason] = useState<NoDeduction | ''>('')
  const [reviewed, setReviewed] = useState<Record<number, 'yes' | 'no'>>({})
  const [overrides, setOverrides] = useState<number[]>([])
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const selectedIds = entries.map(e => Number(e.spool)).filter(Boolean)
  const result = useQuery({ queryKey: ['filament-settlement', id, selectedIds], queryFn: () => archives.settlementPreview(id, selectedIds), retry: false, placeholderData: previous => previous })
  const options = useQuery({ queryKey: ['settlement-spools'], queryFn: () => spools.list(), retry: false })
  const data = result.data
  useEffect(() => {
    if (!data || initialized) return
    const grouped = new Map<number, { spool: string; grams: string }>()
    for (const item of data.suggested) {
      const previous = grouped.get(item.spool_id)
      grouped.set(item.spool_id, { spool: String(item.spool_id), grams: item.grams == null ? '' : String(Number(previous?.grams || 0) + item.grams) })
    }
    const grams = data.grams.measured ?? data.grams.sliced_estimate
    setEntries(grouped.size ? [...grouped.values()] : [{ spool: '', grams: grams == null ? '' : String(grams) }])
    setReason(data.prior_usage ? 'accept_existing' : data.balance_set_after_print.length ? 'balance_already_updated' : '')
    setInitialized(true)
  }, [data, initialized])
  // A newly selected spool may also have a balance that already includes this print.
  useEffect(() => {
    if (data?.balance_set_after_print.length && !data.prior_usage && !overrides.length) setReason('balance_already_updated')
  }, [data?.balance_set_after_print.map(row => row.spool_id).join(','), overrides.length, data?.prior_usage])

  const save = async () => {
    if (!data) return
    setSaving(true); setError('')
    let committed = false
    try {
      const finalReason = data.prior_usage ? 'accept_existing' : reason
      await archives.settleFilament(id, {
        entries: finalReason ? [] : entries.map(e => ({ spool_id: Number(e.spool), grams: Number(e.grams) })),
        counted_usage_ids: data.manual_usage.filter(row => reviewed[row.usage_id] === 'yes').map(row => row.usage_id),
        separate_usage_ids: data.manual_usage.filter(row => reviewed[row.usage_id] === 'no').map(row => row.usage_id),
        no_deduction: finalReason || null, note, deduct_despite_later_balance: finalReason ? [] : overrides.filter(id => selectedIds.includes(id)),
      })
      committed = true; setSaved(true)
      await queryClient.invalidateQueries({ queryKey: ['archives'] })
      await queryClient.invalidateQueries({ queryKey: ['archive-accounting-summary'] })
      onSaved(await archives.get(id))
    } catch (err) {
      if (committed) setError('Filament was settled, but the updated archive could not be loaded.')
      else {
        setError(err instanceof Error ? err.message : 'Could not settle filament. Try again.')
        await result.refetch()
      }
    } finally { setSaving(false) }
  }
  const completeReview = data?.manual_usage.every(row => reviewed[row.usage_id])
  const countedWithoutEntries = (reason || data?.prior_usage) && data?.manual_usage.some(row => reviewed[row.usage_id] === 'yes')
  if (saved) return <section className="mb-4 space-y-2" aria-label="Filament settled"><p role="status">Filament settled.</p>{error && <p role="alert">{error}</p>}<Button onClick={async () => {
    try { onSaved(await archives.get(id)) } catch { setError('The updated archive could not be loaded. Try again.') }
  }}>Refresh archive</Button></section>
  return <section aria-label="Settle filament" className="mb-4 space-y-3 rounded-md border border-amber-500/40 p-3 text-sm">
    <h3>Settle filament</h3>
    {result.isLoading ? <p role="status">Loading filament details…</p> : result.isError ? <p role="alert">Could not load filament details. <button onClick={() => result.refetch()}>Retry</button></p> : data && <>
      <p>{data.cause.title}</p><p>{data.cause.next_step}</p>
      {!data.settleable ? <p>{data.not_settleable}</p> : <>
        {data.job_id == null && <p>Manual use cannot be matched to a print that has no job. If you already took this print's filament off a spool by hand, choose 'No further deduction: recorded by hand'.</p>}
        {data.prior_usage ? <p>Keep the existing deduction</p> : <>
          <label className="block">Deduction<select className={inputClass} value={reason} onChange={e => setReason(e.target.value as NoDeduction | '')}>
            <option value="">Deduct filament</option>{reasons.map(([value, label]) => <option key={value} value={value}>No further deduction: {label.toLowerCase()}</option>)}
          </select></label>
          {!reason && <>
            {options.isError && <p role="alert">Could not load spools. <button onClick={() => options.refetch()}>Retry</button></p>}
            {entries.map((entry, index) => <div key={index} className="grid grid-cols-2 gap-2">
              <label>Spool {index + 1}<select className={inputClass} value={entry.spool} onChange={e => setEntries(entries.map((row, i) => i === index ? { ...row, spool: e.target.value } : row))}>
                <option value="">Choose a spool</option>{(options.data || []).map(spool => <option key={spool.id} value={spool.id}>Spool {spool.id}{spool.color_name ? ` · ${spool.color_name}` : ''}</option>)}
              </select></label>
              <label>Grams {index + 1}<input className={inputClass} type="number" min="0.01" max="10000" step="0.01" value={entry.grams} onChange={e => setEntries(entries.map((row, i) => i === index ? { ...row, grams: e.target.value } : row))} /></label>
              {entries.length > 1 && <Button variant="secondary" onClick={() => setEntries(entries.filter((_, i) => i !== index))}>Remove spool {index + 1}</Button>}
            </div>)}
            {entries.length < 16 && <Button variant="secondary" onClick={() => setEntries([...entries, { spool: '', grams: '' }])}>Add spool</Button>}
          </>}
          {data.balance_set_after_print.map(row => <label key={row.spool_id} className="block">
            <input type="checkbox" checked={overrides.includes(row.spool_id)} onChange={e => { setOverrides(e.target.checked ? [...overrides, row.spool_id] : overrides.filter(id => id !== row.spool_id)); if (e.target.checked) setReason('') }} />{' '}
            Spool {row.spool_id} was {row.how} on {formatDate(row.at)}. Deduct anyway.
          </label>)}
        </>}
        <ManualReview data={data} reviewed={reviewed} onChange={(id, value) => setReviewed({ ...reviewed, [id]: value })} />
        <label className="block">Note (optional)<input className={inputClass} maxLength={200} value={note} onChange={e => setNote(e.target.value)} /></label>
      </>}
    </>}
    {countedWithoutEntries && <p role="status">{data?.prior_usage
      ? 'Keeping the existing deduction cannot include additional manual use. Go back to review this print before settling.'
      : 'Counted manual use needs a spool and total grams. Choose Deduct filament and enter the total; only the difference is deducted.'}</p>}
    {error && <p role="alert">{error}</p>}
    <div className="flex gap-2"><Button variant="secondary" disabled={saving} onClick={onCancel}>Back</Button>
      {data?.settleable && <Button disabled={saving || result.isFetching || result.isError || !completeReview || !!countedWithoutEntries || (!data.prior_usage && !reason && entries.some(e => !e.spool || !(Number(e.grams) > 0)))} onClick={save}>{saving ? 'Saving…' : data.prior_usage ? 'Keep the existing deduction' : 'Confirm settlement'}</Button>}
    </div>
  </section>
}

function ManualReview({ data, reviewed, onChange }: { data: SettlementPreview; reviewed: Record<number, string>; onChange: (id: number, value: 'yes' | 'no') => void }) {
  return <>{data.manual_usage.map(row => <fieldset key={row.usage_id} className="rounded-md border border-[var(--brand-card-border)] p-2">
    <legend>Was this for this print?</legend>
    <p>{row.grams} g from spool {row.spool_id} on {formatDate(row.used_at)}. {row.notes}</p>
    {(['yes', 'no'] as const).map(value => <label key={value} className="mr-4"><input type="radio" name={`manual-${row.usage_id}`} checked={reviewed[row.usage_id] === value} onChange={() => onChange(row.usage_id, value)} /> {value === 'yes' ? 'Yes' : 'No'}</label>)}
  </fieldset>)}</>
}
