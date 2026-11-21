import type { CountDetails } from '../../api/production'

export function inspectionDetails(form: HTMLFormElement): CountDetails {
  const fields = new FormData(form)
  const counted_by = String(fields.get('counted_by') ?? '').trim()
  const note = String(fields.get('note') ?? '').trim()
  return { ...(counted_by && { counted_by }), ...(note && { note }) }
}

export function InspectionFields({disabled}: {disabled: boolean}) {
  const input = 'block w-full rounded border border-[var(--brand-card-border)] bg-[var(--brand-content-bg)] p-2'
  return <fieldset disabled={disabled} className="space-y-2">
    <legend>Optional inspection details</legend>
    <label className="block">Counted by<input className={input} name="counted_by" maxLength={120}/></label>
    <label className="block">Note<input className={input} name="note" maxLength={500}/></label>
  </fieldset>
}

export function CountSummary({counted_by, note}: CountDetails) {
  return <>{counted_by && <span className="block break-words">Counted by {counted_by}</span>}
    {note && <span className="block break-words">Note: {note}</span>}</>
}
