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
        <DollarSign size={18} className="text-print-400" />
        <h2 className="text-lg font-display font-semibold">Cost Chargebacks</h2>
      </div>

      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <div>
          <label htmlFor="cb-start" className="text-xs text-farm-400 block mb-1">Start Date</label>
          <input id="cb-start" type="date" value={startDate} onChange={e => setStartDate(e.target.value)}
            className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-1.5 text-sm" />
        </div>
        <div>
          <label htmlFor="cb-end" className="text-xs text-farm-400 block mb-1">End Date</label>
          <input id="cb-end" type="date" value={endDate} onChange={e => setEndDate(e.target.value)}
            className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-1.5 text-sm" />
        </div>
      </div>

      {isLoading && <p className="text-sm text-farm-500 py-4">Loading...</p>}

      {data && data.length === 0 && (
        <p className="text-sm text-farm-500 py-4">No chargeback data for this period.</p>
      )}

      {data && data.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="border-b border-farm-700">
              <tr>
                <th scope="col" className="text-left py-2 px-3 text-xs font-medium text-farm-400">User</th>
                <th scope="col" className="text-right py-2 px-3 text-xs font-medium text-farm-400">Jobs</th>
                <th scope="col" className="text-right py-2 px-3 text-xs font-medium text-farm-400">Hours</th>
                <th scope="col" className="text-right py-2 px-3 text-xs font-medium text-farm-400">Grams</th>
                <th scope="col" className="text-right py-2 px-3 text-xs font-medium text-farm-400">Cost</th>
              </tr>
            </thead>
            <tbody>
              {data.map(row => (
                <tr key={row.user_id} className="border-b border-farm-800">
                  <td className="py-2 px-3 text-farm-200">{row.username || `User #${row.user_id}`}</td>
                  <td className="py-2 px-3 text-right text-farm-300">{row.total_jobs}</td>
                  <td className="py-2 px-3 text-right text-farm-300">{row.total_hours?.toFixed(1) || '—'}</td>
                  <td className="py-2 px-3 text-right text-farm-300">{row.total_grams?.toFixed(0) || '—'}g</td>
                  <td className="py-2 px-3 text-right text-farm-200 font-medium">${row.total_cost?.toFixed(2) || '0.00'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
