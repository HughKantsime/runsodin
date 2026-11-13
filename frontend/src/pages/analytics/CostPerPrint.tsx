import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download } from 'lucide-react'
import Table from '../../components/ui/Table'
import { analytics } from '../../api'
import type { CostPerPrintRow } from '../../api/reporting'
import { useLicense } from '../../LicenseContext'
import { getCurrentUser } from '../../permissions'

const money = (value: number | null) => value == null ? '—' : `$${value.toFixed(2)}`

// "12.5% over estimate" / "3% under estimate": how the prints' actual cost compares with their estimates.
export function varianceText(percent: number | null): string {
  if (percent == null) return '—'
  if (percent === 0) return 'Same as estimate'
  return `${Math.abs(percent)}% ${percent > 0 ? 'over' : 'under'} estimate`
}

// The first day of a "last N days" range, as a local calendar date (YYYY-MM-DD).
export function rangeStart(days: number, now = new Date()): string {
  const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - days)
  return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`
}

function Row({ row, group, minimum }: { row: CostPerPrintRow; group: 'model' | 'product'; minimum: number }) {
  const name = row.name ?? (group === 'model' ? 'Prints without a model' : 'Prints without a product')
  const left = [row.jobs_partial > 0 && `${row.jobs_partial} with missing records`, row.jobs_without_usage > 0 && `${row.jobs_without_usage} with no usage recorded`].filter(Boolean).join(', ')
  return <tr className="border-t border-[var(--brand-card-border)]">
    <td className="py-1.5 pr-3 text-[var(--brand-text-primary)] break-words">
      {name}
      <span className="block text-xs text-[var(--brand-text-muted)]">{row.jobs} completed{left ? ` (left out: ${left})` : ''}</span>
    </td>
    {row.average_actual_per_piece == null
      ? <td colSpan={5} className="py-1.5 text-xs text-[var(--brand-text-muted)]">Needs {minimum} completed prints with full cost records (has {row.jobs_with_full_cost})</td>
      : <>
        <td className="py-1.5 text-right font-mono text-[var(--brand-text-muted)]">{row.pieces}</td>
        <td className="py-1.5 text-right font-mono text-[var(--brand-text-primary)]">{money(row.average_actual_per_piece)}</td>
        <td className="py-1.5 text-right font-mono text-[var(--brand-text-muted)]">{money(row.average_estimate_per_piece)}</td>
        <td className="py-1.5 text-right text-[var(--brand-text-muted)]">{varianceText(row.variance_percent)}</td>
        <td className="py-1.5 text-right font-mono text-[var(--brand-text-muted)]">{row.failure_share_percent == null ? '—' : `${row.failure_share_percent}%`}</td>
      </>}
  </tr>
}

// Analytics: what completed prints actually cost per piece, by model or product, next to their
// estimates. Nothing is shown until pricing has been saved.
export default function CostPerPrint({ days }: { days: number }) {
  const [group, setGroup] = useState<'model' | 'product'>('model')
  const from = rangeStart(days)
  const mayExport = useLicense().hasFeature('csv_export') && getCurrentUser()?.role !== 'viewer'
  const { data } = useQuery({ queryKey: ['cost-per-print', group, from], queryFn: () => analytics.costPerPrint(group, from), retry: false })
  if (!data || !data.configured) return null
  return <section aria-label="Cost per print" className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
    <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
      <h3 className="text-sm font-semibold text-[var(--brand-text-secondary)]">Cost per print</h3>
      <div className="flex flex-wrap items-center gap-2">
        <select aria-label="Group cost per print by" value={group} onChange={event => setGroup(event.target.value as 'model' | 'product')}
          className="bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md px-2 py-1 text-sm text-[var(--brand-text-secondary)]">
          <option value="model">By model</option>
          <option value="product">By product</option>
        </select>
        {mayExport && <button type="button" onClick={() => { analytics.costPerPrintCsv(group, from).catch(() => {}) }}
          className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md text-sm text-[var(--brand-text-secondary)] transition-colors">
          <Download size={14} /> Export CSV
        </button>}
      </div>
    </div>
    {data.rows.length === 0
      ? <p className="text-sm text-[var(--brand-text-muted)]">No completed prints in this period.</p>
      : <div className="overflow-x-auto" tabIndex={0} role="region" aria-label="Cost per print table">
        <Table className="w-full min-w-[640px] text-sm">
          <thead>
            <tr className="text-xs text-[var(--brand-text-muted)] text-left">
              <th scope="col" className="pb-2 font-medium">{group === 'model' ? 'Model' : 'Product'}</th>
              <th scope="col" className="pb-2 font-medium text-right">Pieces made</th>
              <th scope="col" className="pb-2 font-medium text-right">Actual cost per piece</th>
              <th scope="col" className="pb-2 font-medium text-right">Estimate per piece</th>
              <th scope="col" className="pb-2 font-medium text-right">Actual vs estimate</th>
              <th scope="col" className="pb-2 font-medium text-right">Spent on failed attempts</th>
            </tr>
          </thead>
          <tbody>{data.rows.map(row => <Row key={row.id ?? 'none'} row={row} group={group} minimum={data.minimum_jobs} />)}</tbody>
        </Table>
      </div>}
    <p className="mt-3 text-xs text-[var(--brand-text-muted)]">
      Actual cost is the filament and print hours recorded for each print, failed attempts included, plus labour, packaging and overhead from your pricing settings at current rates. Prints with missing records are left out of the averages.
    </p>
  </section>
}
