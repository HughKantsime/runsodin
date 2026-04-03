import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { DollarSign } from 'lucide-react'
import { chargebacks } from '../../api'

export default function ChargebackReport() {
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')

  const { data, isLoading } = useQuery({
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

      {isLoading && <p className="text-sm text-[var(--brand-text-muted)] py-4">Loading...</p>}

      {data && data.length === 0 && (
        <p className="text-sm text-[var(--brand-text-muted)] py-4">No chargeback data for this period.</p>
      )}

      {data && data.length > 0 && (
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
                  <td className="py-2 px-3 text-right text-[var(--brand-text-secondary)]">{row.total_jobs}</td>
                  <td className="py-2 px-3 text-right text-[var(--brand-text-secondary)]">{row.total_hours?.toFixed(1) || '—'}</td>
                  <td className="py-2 px-3 text-right text-[var(--brand-text-secondary)]">{row.total_grams?.toFixed(0) || '—'}g</td>
                  <td className="py-2 px-3 text-right text-[var(--brand-text-primary)] font-medium">${row.total_cost?.toFixed(2) || '0.00'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
