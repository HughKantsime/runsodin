import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BarChart3, Printer, Clock, CheckCircle, XCircle, Zap, TrendingUp, AlertTriangle, Download } from 'lucide-react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell } from 'recharts'
import clsx from 'clsx'

const API_BASE = '/api'
const getApiHeaders = () => ({
  'X-API-Key': import.meta.env.VITE_API_KEY || '',
  'Authorization': `Bearer ${localStorage.getItem('token')}`,
})

export default function Utilization() {
  const [timeRange, setTimeRange] = useState('30')
  
  const { data: stats, isLoading } = useQuery({
    queryKey: ['stats', timeRange],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/stats?days=${timeRange}`, { headers: getApiHeaders() })
      return res.json()
    }
  })

  const { data: jobsData } = useQuery({
    queryKey: ['print-jobs-util', timeRange],
    queryFn: async () => {
      const res = await fetch(`${API_BASE}/print-jobs?limit=500&days=${timeRange}`, { headers: getApiHeaders() })
      return res.json()
    }
  })

  const printerStats = stats?.printer_stats || []
  const allJobs = jobsData || []

  // Compute fleet-wide metrics
  const fleetHours = printerStats.reduce((s, p) => s + (p.total_hours || 0), 0)
  const fleetJobs = printerStats.reduce((s, p) => s + (p.completed_jobs || 0), 0)
  const fleetFailed = printerStats.reduce((s, p) => s + (p.failed_jobs || 0), 0)
  const fleetAvgUtil = printerStats.length > 0
    ? (printerStats.reduce((s, p) => s + (p.utilization_pct || 0), 0) / printerStats.length).toFixed(1)
    : 0
  const fleetSuccessRate = (fleetJobs + fleetFailed) > 0
    ? ((fleetJobs / (fleetJobs + fleetFailed)) * 100).toFixed(1)
    : 100

  // Jobs per day for chart
  const jobsByDay = {}
  allJobs.filter(j => j.status === 'complete' || j.status === 'failed').forEach(j => {
    const d = (j.completed_at || j.created_at || '').split('T')[0]
    if (d) {
      jobsByDay[d] = (jobsByDay[d] || 0) + 1
    }
  })
  const dailyData = Object.entries(jobsByDay)
    .sort(([a], [b]) => a.localeCompare(b))
    .slice(-parseInt(timeRange))
    .map(([date, count]) => ({ date: date.slice(5), jobs: count }))

  // Utilization bar chart data
  const utilData = printerStats.map(p => ({
    name: p.name?.length > 12 ? p.name.slice(0, 12) + '…' : p.name,
    utilization: p.utilization_pct || 0,
    hours: p.total_hours || 0,
  }))

  // Success pie
  const pieData = [
    { name: 'Completed', value: fleetJobs, color: '#34d399' },
    { name: 'Failed', value: fleetFailed, color: '#f87171' },
  ].filter(d => d.value > 0)

  const exportCSV = () => {
    const rows = [['Printer', 'Jobs', 'Hours', 'Utilization %', 'Success Rate %', 'Failed', 'Avg Job (hrs)']]
    printerStats.forEach(p => {
      rows.push([p.name, p.completed_jobs, p.total_hours, p.utilization_pct, p.success_rate, p.failed_jobs, p.avg_job_hours])
    })
    const csv = rows.map(r => r.join(',')).join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = `utilization-report-${new Date().toISOString().split('T')[0]}.csv`
    a.click(); URL.revokeObjectURL(url)
  }

  if (isLoading) return <div className="text-center py-12 text-farm-500">Loading utilization data...</div>

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div className="flex items-center gap-3">
          <BarChart3 className="text-print-400" size={24} />
          <div>
            <h1 className="text-xl font-bold text-farm-100">Printer Utilization Report</h1>
            <p className="text-sm text-farm-500">Fleet performance and efficiency metrics</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <select value={timeRange} onChange={e => setTimeRange(e.target.value)}
            className="bg-farm-900 border border-farm-700 rounded-lg px-3 py-1.5 text-sm text-farm-300">
            <option value="7">Last 7 days</option>
            <option value="14">Last 14 days</option>
            <option value="30">Last 30 days</option>
          </select>
          <button onClick={exportCSV} className="flex items-center gap-1.5 px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm text-farm-300 transition-colors">
            <Download size={14} /> Export CSV
          </button>
        </div>
      </div>

      {/* Fleet Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 md:gap-3">
        <div className="bg-blue-400/10 rounded-lg p-4 text-center border border-farm-800">
          <TrendingUp size={18} className="mx-auto mb-1 text-blue-400" />
          <div className="text-2xl font-bold tabular-nums text-blue-400">{fleetAvgUtil}%</div>
          <div className="text-xs text-farm-500 uppercase tracking-wide">Avg Utilization</div>
        </div>
        <div className="bg-emerald-400/10 rounded-lg p-4 text-center border border-farm-800">
          <Clock size={18} className="mx-auto mb-1 text-emerald-400" />
          <div className="text-2xl font-bold tabular-nums text-emerald-400">{Math.round(fleetHours)}h</div>
          <div className="text-xs text-farm-500 uppercase tracking-wide">Total Print Hours</div>
        </div>
        <div className="bg-purple-400/10 rounded-lg p-4 text-center border border-farm-800">
          <Printer size={18} className="mx-auto mb-1 text-purple-400" />
          <div className="text-2xl font-bold tabular-nums text-purple-400">{fleetJobs}</div>
          <div className="text-xs text-farm-500 uppercase tracking-wide">Jobs Completed</div>
        </div>
        <div className="bg-green-400/10 rounded-lg p-4 text-center border border-farm-800">
          <CheckCircle size={18} className="mx-auto mb-1 text-green-400" />
          <div className="text-2xl font-bold tabular-nums text-green-400">{fleetSuccessRate}%</div>
          <div className="text-xs text-farm-500 uppercase tracking-wide">Success Rate</div>
        </div>
        <div className="bg-red-400/10 rounded-lg p-4 text-center border border-farm-800">
          <XCircle size={18} className="mx-auto mb-1 text-red-400" />
          <div className="text-2xl font-bold tabular-nums text-red-400">{fleetFailed}</div>
          <div className="text-xs text-farm-500 uppercase tracking-wide">Failed Prints</div>
        </div>
      </div>

      {/* Charts Row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Utilization by Printer */}
        <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
          <h3 className="text-sm font-semibold text-farm-300 mb-4">Utilization by Printer</h3>
          {utilData.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <BarChart data={utilData} layout="vertical" margin={{ left: 10 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                <XAxis type="number" domain={[0, 100]} tickFormatter={v => v + '%'} stroke="#64748b" fontSize={11} />
                <YAxis type="category" dataKey="name" width={100} stroke="#64748b" fontSize={11} />
                <Tooltip contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: 8, fontSize: 12 }}
                  formatter={(v, name) => [name === 'utilization' ? v + '%' : v + 'h', name === 'utilization' ? 'Utilization' : 'Hours']} />
                <Bar dataKey="utilization" fill="#60a5fa" radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : <div className="text-farm-600 text-sm text-center py-12">No printer data available</div>}
        </div>

        {/* Jobs per Day */}
        <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
          <h3 className="text-sm font-semibold text-farm-300 mb-4">Jobs Completed per Day</h3>
          {dailyData.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <BarChart data={dailyData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                <XAxis dataKey="date" stroke="#64748b" fontSize={11} />
                <YAxis stroke="#64748b" fontSize={11} allowDecimals={false} />
                <Tooltip contentStyle={{ background: '#1e293b', border: '1px solid #334155', borderRadius: 8, fontSize: 12 }} />
                <Bar dataKey="jobs" fill="#34d399" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : <div className="text-farm-600 text-sm text-center py-12">No job data available</div>}
        </div>
      </div>

      {/* Success/Failure Pie + Per-Printer Table */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Pie */}
        <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
          <h3 className="text-sm font-semibold text-farm-300 mb-4">Success vs Failure</h3>
          {pieData.length > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
              <PieChart>
                <Pie data={pieData} dataKey="value" cx="50%" cy="50%" outerRadius={80} label={({ name, value }) => `${name}: ${value}`}>
                  {pieData.map((entry, i) => <Cell key={i} fill={entry.color} />)}
                </Pie>
                <Tooltip />
              </PieChart>
            </ResponsiveContainer>
          ) : <div className="text-farm-600 text-sm text-center py-12">No data</div>}
        </div>

        {/* Per-Printer Table */}
        <div className="lg:col-span-2 bg-farm-900 rounded-lg border border-farm-800 p-4 overflow-x-auto">
          <h3 className="text-sm font-semibold text-farm-300 mb-4">Per-Printer Breakdown</h3>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-farm-500 text-xs uppercase tracking-wide border-b border-farm-800">
                <th className="text-left py-2 px-2">Printer</th>
                <th className="text-right py-2 px-2">Jobs</th>
                <th className="text-right py-2 px-2">Hours</th>
                <th className="text-right py-2 px-2">Util %</th>
                <th className="text-right py-2 px-2">Success</th>
                <th className="text-right py-2 px-2">Avg Job</th>
              </tr>
            </thead>
            <tbody>
              {printerStats.map(p => (
                <tr key={p.id} className="border-b border-farm-800/50 hover:bg-farm-800/30">
                  <td className="py-2 px-2 text-farm-200 font-medium">{p.name}</td>
                  <td className="py-2 px-2 text-right tabular-nums text-farm-300">{p.completed_jobs}</td>
                  <td className="py-2 px-2 text-right tabular-nums text-farm-300">{p.total_hours}h</td>
                  <td className="py-2 px-2 text-right tabular-nums">
                    <span className={clsx('font-medium', p.utilization_pct > 50 ? 'text-emerald-400' : p.utilization_pct > 20 ? 'text-yellow-400' : 'text-red-400')}>
                      {p.utilization_pct}%
                    </span>
                  </td>
                  <td className="py-2 px-2 text-right tabular-nums">
                    <span className={clsx('font-medium', p.success_rate >= 95 ? 'text-emerald-400' : p.success_rate >= 80 ? 'text-yellow-400' : 'text-red-400')}>
                      {p.success_rate}%
                    </span>
                  </td>
                  <td className="py-2 px-2 text-right tabular-nums text-farm-400">{p.avg_job_hours}h</td>
                </tr>
              ))}
              {printerStats.length === 0 && (
                <tr><td colSpan={6} className="text-center py-8 text-farm-600">No printer utilization data yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
