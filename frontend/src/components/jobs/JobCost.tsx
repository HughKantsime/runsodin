import { createContext, useContext, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { pricingConfig } from '../../api'
import { jobCost, type CostUnknownReason, type JobCost as Cost } from '../../api/jobCost'

const FINISHED = ['completed', 'failed', 'cancelled']
const money = (value: number) => `$${value.toFixed(2)}`
const hours = (value: number) => `${Number(value.toFixed(2))} h`

const UNKNOWN_TEXT: Record<CostUnknownReason, string> = {
  usage_not_recorded: 'Filament use was not recorded for a print attempt',
  spool_price_missing: 'A spool had no purchase price when the print started',
  spool_weight_missing: 'A spool had no weight on record when the print started',
  price_not_recorded: "A spool's price was not saved when the print started",
  print_time_not_recorded: 'Print time was not recorded for a print attempt',
  attempt_not_finished: 'A print attempt has no recorded end',
  settings_incomplete: 'Pricing settings are incomplete',
}
const SOURCE_TEXT: Record<string, string> = { measured: 'printer reading', sliced_estimate: 'from the sliced file' }

// "Estimate $A, actual $B" in one line; a partial total is always "at least".
export function costSummary(cost: Cost): string {
  const estimate = cost.estimate != null ? `Estimate ${money(cost.estimate)}` : 'No estimate'
  if (cost.state === 'none') return `${estimate}. No usage recorded, so there is no actual cost.`
  if (cost.state === 'partial') return `${estimate}, actual at least ${money(cost.total ?? 0)}`
  return `${estimate}, actual ${money(cost.total ?? 0)}`
}

function difference(cost: Cost): string | null {
  if (cost.state !== 'complete' || cost.difference == null) return null
  if (cost.difference === 0) return 'Same as the estimate'
  return cost.difference > 0 ? `${money(cost.difference)} more than the estimate` : `${money(-cost.difference)} less than the estimate`
}

function Line({ label, value }: { label: string; value: string }) {
  return <li className="flex justify-between gap-3"><span className="min-w-0 break-words">{label}</span><span className="font-mono shrink-0">{value}</span></li>
}

function Breakdown({ cost }: { cost: Cost }) {
  const failed = (cost.attempts ?? []).filter(attempt => attempt.outcome === 'failed' || attempt.outcome === 'cancelled')
  const settings = cost.from_settings
  return <div className="mt-2 space-y-2 text-xs">
    <div>
      <div className="font-medium">Filament used (recorded)</div>
      <ul className="space-y-0.5">
        {(cost.material?.spools ?? []).map((spool, index) => <Line key={`${spool.spool_id}-${index}`}
          label={`${spool.name || 'Spool'}${spool.spool_id != null ? ` (spool ${spool.spool_id})` : ''}: ${Number(spool.grams.toFixed(1))} g${spool.provenance && SOURCE_TEXT[spool.provenance] ? `, ${SOURCE_TEXT[spool.provenance]}` : ''}`}
          value={spool.amount == null ? 'unknown' : money(spool.amount)} />)}
        {(cost.material?.spools ?? []).length === 0 && <li>No filament use on record</li>}
      </ul>
    </div>
    {cost.machine && <div>
      <div className="font-medium">Machine time (recorded hours, at current rates)</div>
      <ul><Line label={`${hours(cost.machine.hours)}${cost.machine.rate_per_hour != null ? ` at ${money(cost.machine.rate_per_hour)} per hour` : ''}`}
        value={cost.machine.complete ? money(cost.machine.amount) : cost.machine.amount > 0 ? `at least ${money(cost.machine.amount)}` : 'unknown'} /></ul>
    </div>}
    {settings && <div>
      <div className="font-medium">From settings (not measured, at current rates)</div>
      {settings.complete ? <ul className="space-y-0.5">
        <Line label="Labour" value={money(settings.labor ?? 0)} />
        <Line label="Packaging" value={money(settings.packaging ?? 0)} />
        <Line label="Other costs" value={money(settings.other ?? 0)} />
        <Line label="Overhead" value={money(settings.overhead ?? 0)} />
      </ul> : <p>unknown</p>}
    </div>}
    {failed.length > 0 && <div>
      <div className="font-medium">Failed or cancelled attempts (already counted above)</div>
      <ul className="space-y-0.5">
        {failed.map(attempt => <Line key={attempt.id}
          label={`${attempt.outcome === 'failed' ? 'Failed' : 'Cancelled'} attempt${attempt.hours != null ? `, ${hours(attempt.hours)}` : ''}`}
          value={attempt.complete ? money(attempt.amount) : `at least ${money(attempt.amount)}`} />)}
      </ul>
    </div>}
    <p className="text-[var(--brand-text-muted)]">The estimate's failure allowance is not added: real failed attempts are counted instead.</p>
  </div>
}

// detail: the job detail card (summary first, breakdown on click). Otherwise the opened row panel.
function CostBody({ jobId, detail }: { jobId: number; detail: boolean }) {
  const [open, setOpen] = useState(!detail)
  const query = useQuery({ queryKey: ['job-cost', jobId], queryFn: () => jobCost.get(jobId), retry: false, staleTime: 60000 })
  if (query.isLoading) return detail ? null : <p role="status" className="text-xs">Loading cost...</p>
  if (query.isError) return detail ? null : <p role="status" className="text-xs">Cost unavailable.</p>
  const cost = query.data
  if (!cost || !cost.configured || !cost.finished) return null
  const reasons = [...new Set((cost.unknown ?? []).map(item => UNKNOWN_TEXT[item.reason] ?? 'A cost was not recorded'))]
  const delta = difference(cost)
  return <section aria-label="Print cost" className={detail ? 'bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 text-sm break-words' : 'text-sm break-words'}>
    {detail && <h4 className="text-[var(--brand-text-secondary)] text-xs uppercase tracking-wide mb-2">Cost</h4>}
    <p>{costSummary(cost)}</p>
    {delta && <p className="text-xs text-[var(--brand-text-muted)]">{delta}</p>}
    {cost.state === 'partial' && <div className="text-xs text-[var(--brand-text-muted)]">
      <p>Not the full cost. Unknown:</p>
      <ul className="list-disc pl-4">{reasons.map(reason => <li key={reason}>{reason}</li>)}</ul>
    </div>}
    {cost.state !== 'none' && <>
      {detail && <button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}
        className="block min-h-[24px] text-xs underline text-[var(--brand-text-muted)]">{open ? 'Hide breakdown' : 'Show breakdown'}</button>}
      {open && <Breakdown cost={cost} />}
    </>}
  </section>
}

// Whether pricing settings have been saved, read once by the page that lists jobs (Jobs.tsx) so
// that rows do not each ask for it. Costs stay hidden until this is true.
export const PricingConfigured = createContext(false)

// What a finished job cost to print, next to its estimate. Hidden until pricing has been saved
// (the same `configured` rule as the Models page) and for jobs that have not finished.
// compact: a list row shows a "Cost" toggle and loads the figures only when it is opened.
export default function JobCost({ jobId, status, compact = false }: { jobId: number; status: string; compact?: boolean }) {
  if (!jobId || !FINISHED.includes(status)) return null
  return compact ? <RowCost jobId={jobId} /> : <DetailCost jobId={jobId} />
}

function DetailCost({ jobId }: { jobId: number }) {
  const { data: settings } = useQuery({ queryKey: ['pricing-config'], queryFn: () => pricingConfig.get(), staleTime: 60000 })
  if ((settings as { configured?: boolean } | undefined)?.configured !== true) return null
  return <CostBody jobId={jobId} detail />
}

function RowCost({ jobId }: { jobId: number }) {
  const [open, setOpen] = useState(false)
  if (!useContext(PricingConfigured)) return null
  return <div>
    <button type="button" aria-expanded={open} onClick={() => setOpen(value => !value)}
      className="block min-h-[24px] text-xs underline text-[var(--brand-text-muted)]">{open ? 'Hide cost' : 'Cost'}</button>
    {open && <CostBody jobId={jobId} detail={false} />}
  </div>
}
