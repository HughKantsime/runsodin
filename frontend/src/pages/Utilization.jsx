import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BarChart3, Printer, Clock, CheckCircle, XCircle, TrendingUp, Download } from 'lucide-react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell } from 'recharts'
import { stats, printJobs } from '../api'

export default function Utilization() {
  const [timeRange, setTimeRange] = useState(30)

  const { data: statsData, isLoading } = useQuery({
    queryKey: ['stats', timeRange],
    queryFn: () => stats.get(timeRange),
  })

  const { data: jobsData } = useQuery({
    queryKey: ['print-jobs-util', timeRange],
    queryFn: () => printJobs.list({ limit: 500, days: timeRange }),
  })

  const printerStats = statsData?.printer_stats || []
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
    if (d) jobsByDay[d] = (jobsByDay[d] || 0) + 1
  })
  const dailyData = Object.entries(jobsByDay)
    .sort(([a], [b]) => a.localeCompare(b))
    .slice(-timeRange)
    .map(([date, count]) => ({
      date: new Date(date + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
      jobs: count,
    }))

  // Utilization bar chart data
  const utilData = printerStats.map(p => ({
    name: p.name?.length > 14 ? p.name.slice(0, 14) + '…' : p.name,
    utilization: p.utilization_pct || 0,
    hours: p.total_hours || 0,
  }))

  // Success pie
  const pieData = [
    { name: 'Completed', value: fleetJobs, color: '#22C55E' },
    { name: 'Failed', value: fleetFailed, color: '#EF4444' },
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

  const DATE_RANGES = [
    { label: '7d', value: 7 },
    { label: '14d', value: 14 },
    { label: '30d', value: 30 },
  ]

  if (isLoading) {
    return (
      <div className="p-4 md:p-6">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-farm-800 rounded-lg w-48"></div>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
            {[...Array(5)].map((_, i) => (
              <div key={i} className="h-24 bg-farm-800/50 rounded-xl"></div>
            ))}
          </div>
          <div className="h-64 bg-farm-800/50 rounded-xl"></div>
        </div>
      </div>
    )
  }

  return (
    <div className="p-4 md:p-6 space-y-5 md:space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div>
          <h1 className="text-xl md:text-2xl font-display font-bold">Utilization</h1>
          <p className="text-farm-500 text-sm mt-0.5">Fleet performance and efficiency metrics</p>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1">
            {DATE_RANGES.map(r => (
              <button
                key={r.value}
                onClick={() => setTimeRange(r.value)}
                className={`px-3 py-1.5 rounded-lg text-sm transition-colors ${
                  timeRange === r.value
                    ? 'bg-print-600 text-white font-medium'
                    : 'bg-farm-800 text-farm-400 hover:bg-farm-700'
                }`}
              >
                {r.label}
              </button>
            ))}
          </div>
          <button onClick={exportCSV} className="flex items-center gap-1.5 px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm text-farm-400 transition-colors">
            <Download size={14} /> CSV
          </button>
        </div>
      </div>

      {/* Fleet Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 md:gap-4">
        <div className="relative overflow-hidden rounded-xl border border-farm-800 p-4" style={{ backgroundColor: 'var(--chart-card-bg)' }}>
          <div className="flex items-start justify-between">
            <div>
              <p className="text-xs font-medium text-farm-400 uppercase tracking-wider">Avg Utilization</p>
              <p className="text-2xl font-display font-bold mt-1 text-blue-400">{fleetAvgUtil}%</p>
            </div>
            <div className="p-2.5 rounded-lg bg-blue-900/30"><TrendingUp size={18} className="text-blue-400" /></div>
          </div>
        </div>
        <div className="relative overflow-hidden rounded-xl border border-farm-800 p-4" style={{ backgroundColor: 'var(--chart-card-bg)' }}>
          <div className="flex items-start justify-between">
            <div>
              <p className="text-xs font-medium text-farm-400 uppercase tracking-wider">Print Hours</p>
              <p className="text-2xl font-display font-bold mt-1 text-cyan-400">{Math.round(fleetHours)}h</p>
            </div>
            <div className="p-2.5 rounded-lg bg-farm-800"><Clock size={18} className="text-cyan-400" /></div>
          </div>
        </div>
        <div className="relative overflow-hidden rounded-xl border border-farm-800 p-4" style={{ backgroundColor: 'var(--chart-card-bg)' }}>
          <div className="flex items-start justify-between">
            <div>
              <p className="text-xs font-medium text-farm-400 uppercase tracking-wider">Jobs Completed</p>
              <p className="text-2xl font-display font-bold mt-1 text-purple-400">{fleetJobs}</p>
            </div>
            <div className="p-2.5 rounded-lg bg-purple-900/30"><Printer size={18} className="text-purple-400" /></div>
          </div>
        </div>
        <div className="relative overflow-hidden rounded-xl border border-farm-800 p-4" style={{ backgroundColor: 'var(--chart-card-bg)' }}>
          <div className="flex items-start justify-between">
            <div>
              <p className="text-xs font-medium text-farm-400 uppercase tracking-wider">Success Rate</p>
              <p className="text-2xl font-display font-bold mt-1 text-green-400">{fleetSuccessRate}%</p>
            </div>
            <div className="p-2.5 rounded-lg bg-green-900/30"><CheckCircle size={18} className="text-green-400" /></div>
          </div>
        </div>
        <div className="relative overflow-hidden rounded-xl border border-farm-800 p-4" style={{ backgroundColor: 'var(--chart-card-bg)' }}>
          <div className="flex items-start justify-between">
            <div>
              <p className="text-xs font-medium text-farm-400 uppercase tracking-wider">Failed Prints</p>
              <p className="text-2xl font-display font-bold mt-1 text-red-400">{fleetFailed}</p>
            </div>
            <div className="p-2.5 rounded-lg bg-red-900/30"><XCircle size={18} className="text-red-400" /></div>
          </div>
        </div>
      </div>

      {/* Charts Row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 md:gap-6">
        {/* Utilization by Printer */}
        <div className="rounded-xl border border-farm-800 p-5" style={{ backgroundColor: 'var(--chart-card-bg)' }}>
          <div className="flex items-center gap-2 mb-4">
            <BarChart3 size={18} className="text-blue-400" />
            <h3 className="font-display font-semibold">Utilization by Printer</h3>
          </div>
          {utilData.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <BarChart data={utilData} layout="vertical" margin={{ left: 10 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
                <XAxis type="number" domain={[0, 100]} tick={{ fill: 'var(--chart-axis)', fontSize: 10 }} axisLine={{ stroke: 'var(--chart-axis-line)' }} tickLine={false} tickFormatter={v => v + '%'} />
                <YAxis type="category" dataKey="name" width={100} tick={{ fill: 'var(--chart-axis)', fontSize: 10 }} axisLine={false} tickLine={false} />
                <Tooltip
                  contentStyle={{ backgroundColor: 'var(--chart-tooltip-bg)', border: '1px solid var(--chart-tooltip-border)', borderRadius: '12px', fontSize: '12px', boxShadow: 'var(--chart-tooltip-shadow)' }}
                  formatter={(v, name) => [name === 'utilization' ? v + '%' : v + 'h', name === 'utilization' ? 'Utilization' : 'Hours']}
                />
                <Bar dataKey="utilization" fill="#3B82F6" radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : <div className="text-center py-12 text-farm-500 text-sm">No printer data yet</div>}
        </div>

        {/* Jobs per Day */}
        <div className="rounded-xl border border-farm-800 p-5" style={{ backgroundColor: 'var(--chart-card-bg)' }}>
          <div className="flex items-center gap-2 mb-4">
            <BarChart3 size={18} className="text-green-400" />
            <h3 className="font-display font-semibold">Jobs Completed per Day</h3>
          </div>
          {dailyData.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <BarChart data={dailyData}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
                <XAxis dataKey="date" tick={{ fill: 'var(--chart-axis)', fontSize: 10 }} axisLine={{ stroke: 'var(--chart-axis-line)' }} tickLine={false} />
                <YAxis tick={{ fill: 'var(--chart-axis)', fontSize: 10 }} axisLine={false} tickLine={false} allowDecimals={false} />
                <Tooltip contentStyle={{ backgroundColor: 'var(--chart-tooltip-bg)', border: '1px solid var(--chart-tooltip-border)', borderRadius: '12px', fontSize: '12px', boxShadow: 'var(--chart-tooltip-shadow)' }} />
                <Bar dataKey="jobs" fill="#22C55E" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : <div className="text-center py-12 text-farm-500 text-sm">No job data yet</div>}
        </div>
      </div>

      {/* Success/Failure Pie + Per-Printer Table */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4 md:gap-6">
        {/* Pie */}
        <div className="rounded-xl border border-farm-800 p-5" style={{ backgroundColor: 'var(--chart-card-bg)' }}>
          <div className="flex items-center gap-2 mb-4">
            <CheckCircle size={18} className="text-green-400" />
            <h3 className="font-display font-semibold">Success vs Failure</h3>
          </div>
          {pieData.length > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
              <PieChart>
                <Pie data={pieData} dataKey="value" cx="50%" cy="50%" outerRadius={80} label={({ name, value }) => `${name}: ${value}`}>
                  {pieData.map((entry, i) => <Cell key={i} fill={entry.color} />)}
                </Pie>
                <Tooltip contentStyle={{ backgroundColor: 'var(--chart-tooltip-bg)', border: '1px solid var(--chart-tooltip-border)', borderRadius: '12px', fontSize: '12px', boxShadow: 'var(--chart-tooltip-shadow)' }} />
              </PieChart>
            </ResponsiveContainer>
          ) : <div className="text-center py-12 text-farm-500 text-sm">No job data yet</div>}
        </div>

        {/* Per-Printer Table */}
        <div className="lg:col-span-2 rounded-xl border border-farm-800 p-5 overflow-x-auto" style={{ backgroundColor: 'var(--chart-card-bg)' }}>
          <div className="flex items-center gap-2 mb-4">
            <Printer size={18} className="text-purple-400" />
            <h3 className="font-display font-semibold">Per-Printer Breakdown</h3>
          </div>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-farm-500 text-xs uppercase tracking-wider border-b border-farm-800">
                <th className="text-left py-2 px-2">Printer</th>
                <th className="text-right py-2 px-2">Jobs</th>
                <th className="text-right py-2 px-2">Hours</th>
                <th className="text-right py-2 px-2">Util %</th>
                <th className="text-right py-2 px-2">Success</th>
                <th className="text-right py-2 px-2">Avg Job</th>
              </tr>
            </thead>
            <tbody>
              {printerStats.map(p => {
                const utilColor = (p.utilization_pct || 0) >= 50 ? 'text-green-400' : (p.utilization_pct || 0) >= 20 ? 'text-yellow-400' : 'text-red-400'
                const successColor = (p.success_rate || 0) >= 95 ? 'text-green-400' : (p.success_rate || 0) >= 80 ? 'text-yellow-400' : 'text-red-400'
                return (
                  <tr key={p.id} className="border-b border-farm-800/50 hover:bg-farm-800/30">
                    <td className="py-2 px-2 text-farm-200 font-medium">{p.name}</td>
                    <td className="py-2 px-2 text-right tabular-nums text-farm-300">{p.completed_jobs}</td>
                    <td className="py-2 px-2 text-right tabular-nums text-farm-300">{p.total_hours}h</td>
                    <td className="py-2 px-2 text-right tabular-nums">
                      <span className={`font-medium ${utilColor}`}>{p.utilization_pct}%</span>
                    </td>
                    <td className="py-2 px-2 text-right tabular-nums">
                      <span className={`font-medium ${successColor}`}>{p.success_rate}%</span>
                    </td>
                    <td className="py-2 px-2 text-right tabular-nums text-farm-400">{p.avg_job_hours}h</td>
                  </tr>
                )
              })}
              {printerStats.length === 0 && (
                <tr><td colSpan={6} className="text-center py-8 text-farm-500 text-sm">No printer utilization data yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
