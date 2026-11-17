import { useEffect, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { archives, spools } from '../../api'
import type { Archive, NoDeduction, SettlementPreview } from '../../types'
import { canDo } from '../../permissions'
import { Button, Modal } from '../../components/ui'
import CheckTarget from '../../components/ui/CheckTarget'
import { formatDate } from '../../utils/shared'

const reasons: [NoDeduction, string][] = [
  ['nothing_extruded', 'Nothing was extruded'], ['recorded_by_hand', 'Recorded by hand'],
  ['spool_not_tracked', 'Spool is not tracked'], ['balance_already_updated', 'Balance already updated'],
]
const inputClass = 'min-h-11 w-full rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-input-bg)] p-2'

export default function FilamentAccounting({ archive }: { archive: Archive }) {
  const [editing, setEditing] = useState(false)
  const [busy, setBusy] = useState(false)
  const [reloadRequired, setReloadRequired] = useState(false)
  const [updated, setUpdated] = useState<Archive | null>(null)
  const shown = updated || archive
  const settledStatus = useRef<HTMLDivElement>(null)
  const attentionStatus = useRef<HTMLDivElement>(null)
  useEffect(() => { if (!editing && reloadRequired) attentionStatus.current?.focus() }, [editing, reloadRequired])
  useEffect(() => { if (!editing && updated?.accounting_settled) settledStatus.current?.focus() }, [editing, updated])
  const needsSettlement = shown.accounting_state === 'unresolved' && shown.settleable === true
  const needsAttention = shown.accounting_state === 'unresolved' || shown.accounting_state === 'reconciliation_required'
  return <>
    {editing && <Modal isOpen onClose={() => { if (!busy) setEditing(false) }} title="Settle filament"><SettlementForm id={archive.id} onBusy={setBusy} onCancel={() => setEditing(false)} onRefresh={value => { if (value) setUpdated(value); setReloadRequired(value === null) }} onSaved={value => { setUpdated(value); setReloadRequired(false); setEditing(false) }} /></Modal>}
    {shown.accounting_settled && <div ref={settledStatus} tabIndex={-1} className="mb-4 text-sm" role="status">
      <p>Filament settled{shown.accounting_settled.by ? ` by ${shown.accounting_settled.by}` : ''} on {formatDate(shown.accounting_settled.at)}:</p>
      {shown.consumption?.map((item, i) => <p key={i}>{item.spool_id != null && item.grams_used != null
        ? `${item.grams_used.toFixed(1)} g from spool ${item.spool_id}${item.deducted_now_g != null ? `; ${item.deducted_now_g.toFixed(1)} g deducted now` : ''}` : item.reason === 'usage_listed_on_another_archive' ? 'The existing deduction is listed on another archive for this job.' : item.reason === 'accept_existing' ? 'No further deduction: existing deduction kept' : `No further deduction${reasons.find(([value]) => value === item.reason)?.[1] ? `: ${reasons.find(([value]) => value === item.reason)?.[1].toLowerCase()}` : ''}`}{item.remaining_unknown ? '. The spool balance is still unknown.' : ''}</p>)}
    </div>}
    {needsAttention && <div ref={attentionStatus} tabIndex={-1} className="mb-4 rounded-md border border-amber-500/40 p-3 text-sm" role="status">
      <p>{shown.accounting_cause?.title || 'This print needs its filament settled'}</p>
      <p>{shown.accounting_cause?.next_step || 'Settle this print'}</p>
      {shown.consumption?.filter(item => item.slot_number != null).map((item, i) => <p key={i}>Slot {item.slot_number}: {item.grams_used != null ? `${item.grams_used.toFixed(1)} g` : 'Quantity unknown'}</p>)}
      {reloadRequired && <><p>Reload this print before trying again.</p><Button onClick={async () => { try { setUpdated(await archives.get(archive.id)); setReloadRequired(false) } catch { /* Keep the reload requirement until a fresh read succeeds. */ } }}>Reload this print</Button></>}
      {needsSettlement && !reloadRequired && canDo('archives.settle_filament') && <Button className="mt-2" onClick={() => setEditing(true)}>Settle filament</Button>}
    </div>}
  </>
}

function SettlementForm({ id, onCancel, onSaved, onRefresh, onBusy }: { onBusy: (busy: boolean) => void; id: number; onCancel: () => void; onRefresh: (archive: Archive | null) => void; onSaved: (archive: Archive) => void }) {
  const queryClient = useQueryClient()
  const [entries, setEntries] = useState<{ spool: string; grams: string }[]>([])
  const [initialized, setInitialized] = useState(false)
  const [reason, setReason] = useState<NoDeduction | ''>('')
  const [reviewed, setReviewed] = useState<Record<number, 'yes' | 'no'>>({})
  const [overrides, setOverrides] = useState<number[]>([])
  const [note, setNote] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  useEffect(() => { onBusy(saving) }, [saving, onBusy])
  const [saved, setSaved] = useState(false)
  const [uncertain, setUncertain] = useState(false)
  const selectedIds = entries.map(e => Number(e.spool)).filter(Boolean)
  const result = useQuery({ queryKey: ['filament-settlement', id, selectedIds], queryFn: () => archives.settlementPreview(id, selectedIds), retry: false, placeholderData: previous => previous })
  const options = useQuery({ queryKey: ['settlement-spools'], queryFn: () => spools.list(), retry: false })
  const data = result.data
  useEffect(() => {
    if (!data || initialized) return
    const grouped = new Map<number, { spool: string; grams: number | null }>()
    for (const item of data.suggested) {
      const previous = grouped.get(item.spool_id)
      grouped.set(item.spool_id, { spool: String(item.spool_id), grams: item.grams == null ? (previous?.grams ?? null) : (previous?.grams ?? 0) + item.grams })
    }
    const grams = data.grams.measured ?? data.grams.sliced_estimate
    setEntries(grouped.size ? [...grouped.values()].map(row => ({ spool: row.spool, grams: row.grams == null ? '' : row.grams.toFixed(1) })) : [{ spool: '', grams: grams == null ? '' : grams.toFixed(1) }])
    setReason(data.prior_usage ? 'accept_existing' : grouped.size > 0 && [...grouped.keys()].every(id => data.balance_set_after_print.some(row => row.spool_id === id && !row.how.startsWith('unknown_'))) ? 'balance_already_updated' : '')
    setInitialized(true)
  }, [data, initialized])
  const later = data?.balance_set_after_print.filter(row => selectedIds.includes(row.spool_id)) || []
  const gramsValue = (value: string) => Number(value.trim().replace(',', '.'))

  const save = async () => {
    if (!data) return
    setSaving(true); setError('')
    let committed = false
    try {
      const finalReason = data.prior_usage ? 'accept_existing' : reason
      await archives.settleFilament(id, {
        entries: finalReason ? [] : entries.map(e => ({ spool_id: Number(e.spool), grams: gramsValue(e.grams) })),
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
        const network = !(err as { status?: number })?.status
        setUncertain(network)
        setError(network ? 'ODIN could not confirm the result. Reload this print before trying again.' : err instanceof Error ? err.message : 'Could not settle filament. Reload this print and try again.')
        await result.refetch()
        try { onRefresh(await archives.get(id)) } catch { setUncertain(true); onRefresh(null) }
        await queryClient.invalidateQueries({ queryKey: ['archives'] })
        await queryClient.invalidateQueries({ queryKey: ['archive-accounting-summary'] })
      }
    } finally { setSaving(false) }
  }
  const completeReview = data?.manual_usage.every(row => reviewed[row.usage_id])
  const countedWithoutEntries = !data?.prior_usage && reason && data?.manual_usage.some(row => reviewed[row.usage_id] === 'yes')
  const validation = !completeReview ? 'Say Yes or No for every manual-use row.'
    : countedWithoutEntries ? 'Counted manual use needs a spool and total grams. Choose Deduct filament and enter the total; only the difference is deducted.'
    : !data?.prior_usage && !reason ? options.isLoading ? 'Loading spools…'
    : options.isError ? 'The spool list could not be loaded. Retry before confirming.'
    : entries.some(e => !options.data?.some(spool => String(spool.id) === e.spool)) ? 'Choose an available spool for every entry.'
    : new Set(selectedIds).size !== selectedIds.length ? 'Choose each spool only once; combine its grams in one entry.'
    : entries.some(e => !Number.isFinite(gramsValue(e.grams)) || gramsValue(e.grams) <= 0 || gramsValue(e.grams) > 10000) ? 'Enter grams greater than zero and no more than 10000 for every spool.'
    : later.some(row => !overrides.includes(row.spool_id)) ? 'Review each later balance: remove that spool or confirm Deduct anyway.' : '' : ''
  if (saved) return <section className="mb-4 space-y-2" aria-label="Filament settled"><p role="status">Filament settled.</p>{error && <p role="alert">{error}</p>}<Button onClick={async () => {
    try { onSaved(await archives.get(id)) } catch { setError('The updated archive could not be loaded. Try again.') }
  }}>Refresh archive</Button></section>
  return <section aria-label="Settle filament" className="mb-4 space-y-3 rounded-md border border-amber-500/40 p-3 text-sm">

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
                <option value="">Choose a spool</option>{(options.data || []).map(spool => <option key={spool.id} value={spool.id}>#{spool.id} {spool.filament_brand} {spool.filament_name}{spool.color_name ? ` · ${spool.color_name}` : ''}</option>)}
              </select></label>
              <label>Grams {index + 1}<input className={inputClass} type="text" inputMode="decimal" value={entry.grams} onChange={e => setEntries(entries.map((row, i) => i === index ? { ...row, grams: e.target.value } : row))} /></label>
              {entries.length > 1 && <Button variant="secondary" onClick={() => setEntries(entries.filter((_, i) => i !== index))}>Remove spool {index + 1}</Button>}
            </div>)}
            {entries.length < 16 && <Button variant="secondary" onClick={() => setEntries([...entries, { spool: '', grams: '' }])}>Add spool</Button>}
          </>}
          {later.map(row => <div key={row.spool_id} className="flex items-center gap-3 min-h-11">
            <CheckTarget touch>
            <input className="h-6 w-6" aria-label={`Deduct anyway from spool ${row.spool_id}`} type="checkbox" checked={overrides.includes(row.spool_id)} onChange={e => { setOverrides(e.target.checked ? [...overrides, row.spool_id] : overrides.filter(id => id !== row.spool_id)); if (e.target.checked) setReason('') }} /></CheckTarget>
            <span>{row.how === 'unknown_print_time' ? `The print time is unknown, so ODIN cannot tell whether spool ${row.spool_id} already includes this use.`
              : row.how === 'unknown_balance_time' ? `The balance time cannot be read, so ODIN cannot tell whether spool ${row.spool_id} already includes this use.`
              : row.how === 'added' ? `Spool ${row.spool_id}: This spool was added after this print ended (${formatDate(row.at)}).`
              : `Spool ${row.spool_id} was ${row.how} on ${formatDate(row.at)}.`} Deduct anyway.</span>
          </div>)}
        </>}
        <ManualReview data={data} reviewed={reviewed} onChange={(id, value) => setReviewed({ ...reviewed, [id]: value })} />
        <label className="block">Note (optional)<input className={inputClass} maxLength={200} value={note} onChange={e => setNote(e.target.value)} /></label>
      </>}
    </>}
    {data?.settleable && validation && <p role="status">{validation}</p>}
    {data && result.isFetching && <p role="status">Refreshing filament details…</p>}
    {error && <p role="alert">{error}</p>}
    {uncertain && <Button onClick={async () => { try { onSaved(await archives.get(id)) } catch { setError('The updated archive could not be loaded. Try again.') } }}>Reload this print</Button>}
    <div className="flex gap-2"><Button variant="secondary" disabled={saving} onClick={onCancel}>Back</Button>
      {data?.settleable && <Button disabled={saving || uncertain || result.isFetching || result.isError || !!validation} onClick={save}>{saving ? 'Saving…' : data.prior_usage ? 'Keep the existing deduction' : 'Confirm settlement'}</Button>}
    </div>
  </section>
}

function ManualReview({ data, reviewed, onChange }: { data: SettlementPreview; reviewed: Record<number, string>; onChange: (id: number, value: 'yes' | 'no') => void }) {
  return <>{data.manual_usage.map(row => <fieldset key={row.usage_id} aria-describedby={`usage-${row.usage_id}`} className="rounded-md border border-[var(--brand-card-border)] p-2">
    <legend>Was this for this print?</legend>
    <p id={`usage-${row.usage_id}`}>{row.grams.toFixed(1)} g from spool {row.spool_id} on {formatDate(row.used_at)}. {row.notes}</p>
    {(['yes', 'no'] as const).map(value => <span key={value} className="mr-4 inline-flex min-h-11 items-center gap-3"><CheckTarget touch><input className="h-6 w-6" aria-label={value === 'yes' ? 'Yes' : 'No'} type="radio" name={`manual-${row.usage_id}`} checked={reviewed[row.usage_id] === value} onChange={() => onChange(row.usage_id, value)} /></CheckTarget> {value === 'yes' ? 'Yes' : 'No'}</span>)}
  </fieldset>)}</>
}
