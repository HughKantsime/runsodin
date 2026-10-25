import { useEffect, useState } from 'react'
import { orders, type OrderMeasuredCosts } from '../../api/orders'

const reasons: Record<string, string> = {
  inventory_shortfall: 'Measured consumption exceeds recorded inventory; reconciliation required',
  missing_measurement: 'Printer consumption was not reported',
  invalid_measurement: 'Printer consumption is invalid',
  missing_historical_valuation: 'Historical valuation evidence is unavailable',
  unresolved_allocation: 'Measured consumption cannot be allocated to one spool',
  unresolved_purchase_evidence: 'Frozen purchase evidence is incomplete',
  currency_mismatch: 'Currencies do not agree',
  valuation_bound_exceeded: 'Valuation exceeds supported bounds',
  mixed_order_plate: 'Plate consumption belongs to multiple orders',
  archive_ownership_unresolved: 'Historical content ownership is unresolved',
  open_attempt: 'Attempt is still open',
  missing_terminal_observation: 'Terminal measurement has not been retained',
  no_participating_attempts: 'No participating physical attempts are recorded',
}

const money = (amount: string | null, currency: string | null) => amount !== null && currency
  ? new Intl.NumberFormat('en-US', { style: 'currency', currency, currencyDisplay: 'code' }).format(Number(amount)).replace(/\u00a0/g, ' ') : 'Unavailable'

export function MeasuredFilamentView({ value }: { value: OrderMeasuredCosts }) {
  const total = money(value.measured_filament_known_subtotal, value.currency)
  return <>
    <p>{value.filament_state === 'fully_measured' ? `Printer-reported estimate: ${total}`
      : value.measured_filament_known_subtotal !== null ? `Known measured filament cost: ${total}` : 'Measured cost unavailable'}</p>
    <p className="text-sm text-[var(--brand-text-secondary)]">Printer-reported estimate.</p>
    {value.filament_state !== 'fully_measured' && <p className="text-sm">Partial coverage: {value.known_attempts} known, {value.unresolved_attempts} unresolved, {value.open_attempts} open attempts.</p>}
    {value.successful_subtotal !== null && <p>Successful attempts: {money(value.successful_subtotal, value.currency)}</p>}
    {value.failed_cancelled_subtotal !== null && <p>Failed / cancelled attempts: {money(value.failed_cancelled_subtotal, value.currency)}</p>}
    {value.reasons.length > 0 && <ul className="text-sm list-disc pl-5">{value.reasons.map(reason => <li key={reason}>{reasons[reason] || 'Evidence unavailable'}</li>)}</ul>}
    {value.attempts.length > 0 && <details><summary className="cursor-pointer">Attempt breakdown</summary>
      <ul className="space-y-2 mt-2 text-sm">{value.attempts.map((attempt, index) => <li key={attempt.attempt_id ?? `unresolved-${index}`}>
        {attempt.attempt_id === null ? 'Unresolved attempt' : `Attempt #${attempt.attempt_id}`}: {attempt.outcome} — {attempt.measurement_basis === 'printer_reported' ? 'Printer-reported estimate' : 'Measurement unavailable'}; {money(attempt.amount, attempt.currency)}
        {attempt.reason && <span> — {reasons[attempt.reason] || 'Evidence unavailable'}</span>}
      </li>)}</ul>
    </details>}
    <p className="text-sm text-[var(--brand-text-secondary)]">Filament only. Production COGS and measured margin are unavailable. BOM estimates are shown separately.</p>
  </>
}

export default function MeasuredFilamentCost({ orderId }: { orderId: number }) {
  const [result, setResult] = useState<{ orderId: number; value?: OrderMeasuredCosts; failed?: boolean } | null>(null)
  useEffect(() => {
    let active = true
    setResult(null)
    orders.measuredCosts(orderId).then(value => { if (active) setResult({ orderId, value }) })
      .catch(() => { if (active) setResult({ orderId, failed: true }) })
    return () => { active = false }
  }, [orderId])
  const current = result?.orderId === orderId ? result : null
  return <section aria-label="Measured filament cost" className="rounded-md border border-[var(--brand-card-border)] p-4 space-y-2">
    <h3 className="font-medium">Measured filament cost</h3>
    {!current ? <p role="status">Loading measured costs…</p> : current.value ? <MeasuredFilamentView value={current.value} />
      : <p>Measured cost unavailable. Financial authority or historical evidence could not be verified.</p>}
  </section>
}
