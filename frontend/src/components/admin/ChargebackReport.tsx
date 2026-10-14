import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { DollarSign } from 'lucide-react'
import { chargebacks } from '../../api'

const currencies = new Set(['USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY', 'CHF', 'NZD'])
const known = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0
function cost(amount: unknown, currency: unknown) {
  return known(amount) && typeof currency === 'string' && currencies.has(currency)
    ? `${currency} ${amount.toFixed(currency === 'JPY' ? 0 : 2)}` : 'Unknown'
}

export default function ChargebackReport() {
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['chargebacks', startDate, endDate],
    queryFn: () => chargebacks.report(startDate || undefined, endDate || undefined),
  })

  return (
    <div>
      <div className="flex items-center gap-2 mb-4">
        <DollarSign size={18} className="text-[var(--brand-primary)]" />
        <h2 className="text-lg font-display font-semibold">Cost Chargebacks</h2>
      </div>

      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <div>
          <label htmlFor="cb-start" className="text-xs text-[var(--brand-text-secondary)] block mb-1">Start Date</label>
          <input id="cb-start" type="date" value={startDate} onChange={e => setStartDate(e.target.value)}
            className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-1.5 text-sm" />
        </div>
        <div>
          <label htmlFor="cb-end" className="text-xs text-[var(--brand-text-secondary)] block mb-1">End Date</label>
          <input id="cb-end" type="date" value={endDate} onChange={e => setEndDate(e.target.value)}
            className="bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-1.5 text-sm" />
        </div>
      </div>

      <p className="text-xs text-[var(--brand-text-secondary)] mb-3">Dates include both selected days, using UTC.</p>

      {isLoading && <p className="text-sm text-[var(--brand-text-muted)] py-4">Loading...</p>}

      {isError && (
        <div role="alert" className="py-3 text-[var(--brand-text-primary)]">
          <p>Unable to load chargeback data.</p>
          <button type="button" onClick={() => void refetch()} className="mt-2 rounded-md border px-3 py-2">Retry</button>
        </div>
      )}

      {!isError && data && data.length === 0 && (
        <p className="text-sm text-[var(--brand-text-muted)] py-4">No chargeback data for this period.</p>
      )}

      {!isError && data && data.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="border-b border-[var(--brand-card-border)]">
              <tr>
                <th scope="col" className="text-left py-2 px-3 text-xs font-medium text-[var(--brand-text-secondary)]">User</th>
                <th scope="col" className="text-right py-2 px-3 text-xs font-medium text-[var(--brand-text-secondary)]">Jobs</th>
                <th scope="col" className="text-right py-2 px-3 text-xs font-medium text-[var(--brand-text-secondary)]">Hours</th>
                <th scope="col" className="text-right py-2 px-3 text-xs font-medium text-[var(--brand-text-secondary)]">Grams</th>
                <th scope="col" className="text-right py-2 px-3 text-xs font-medium text-[var(--brand-text-secondary)]">Cost</th>
              </tr>
            </thead>
            <tbody>
              {data.map(row => (
                <tr key={row.user_id} className="border-b border-[var(--brand-card-border)]">
                  <td className="py-2 px-3 text-[var(--brand-text-primary)]">{row.username || `User #${row.user_id}`}</td>
                  <td className="py-2 px-3 text-right text-[var(--brand-text-secondary)]">{known(row.job_count) && Number.isInteger(row.job_count) ? row.job_count : 'Unknown'}</td>
                  <td className="py-2 px-3 text-right text-[var(--brand-text-secondary)]">{known(row.total_hours) ? row.total_hours.toFixed(1) : 'Unknown'}</td>
                  <td className="py-2 px-3 text-right text-[var(--brand-text-secondary)]">{known(row.total_grams) ? `${row.total_grams.toFixed(0)}g` : 'Unknown'}{Number.isInteger(row.unmeasured_prints) && row.unmeasured_prints > 0 ? ` (+${row.unmeasured_prints} unmeasured)` : ''}</td>
                  <td className="py-2 px-3 text-right text-[var(--brand-text-primary)] font-medium">{cost(row.total_cost, row.currency)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
