import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BarChart3, Printer, Clock, CheckCircle, XCircle, TrendingUp, Download } from 'lucide-react'
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, PieChart, Pie, Cell } from 'recharts'
import clsx from 'clsx'
import { stats, printJobs } from '../../api'

const CHART_TOOLTIP = { backgroundColor: 'var(--chart-tooltip-bg)', border: 'none', borderRadius: '6px', boxShadow: 'var(--chart-tooltip-shadow)', color: 'var(--brand-text-primary)' }

export default function Utilization() {
  const [timeRange, setTimeRange] = useState('30')

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

  if (isLoading) return <div className="text-center py-12 text-[var(--brand-text-muted)]">Loading utilization data...</div>

  return (
    <div className="p-4 md:p-6 space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div className="flex items-center gap-3">
          <BarChart3 className="text-[var(--brand-primary)]" size={24} />
          <div>
            <h1 className="text-xl md:text-2xl font-display font-bold">Printer Utilization Report</h1>
            <p className="text-sm text-[var(--brand-text-muted)]">Fleet performance and efficiency metrics</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <select value={timeRange} onChange={e => setTimeRange(e.target.value)}
            className="bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-1.5 text-sm text-[var(--brand-text-secondary)]">
            <option value="7">Last 7 days</option>
            <option value="14">Last 14 days</option>
            <option value="30">Last 30 days</option>
          </select>
          <button onClick={exportCSV} className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md text-sm text-[var(--brand-text-secondary)] transition-colors">
            <Download size={14} /> Export CSV
          </button>
        </div>
      </div>

      {/* Fleet Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 md:gap-3">
        <div className="bg-blue-900/30 rounded-md p-4 text-center border border-blue-700/30">
          <TrendingUp size={18} className="mx-auto mb-1 text-blue-400" />
          <div className="text-2xl font-bold font-display tabular-nums text-blue-400">{fleetAvgUtil}%</div>
          <div className="text-xs text-[var(--brand-text-muted)] uppercase tracking-wide">Avg Utilization</div>
        </div>
        <div className="bg-emerald-900/30 rounded-md p-4 text-center border border-emerald-700/30">
          <Clock size={18} className="mx-auto mb-1 text-emerald-400" />
          <div className="text-2xl font-bold font-display tabular-nums text-emerald-400">{Math.round(fleetHours)}h</div>
          <div className="text-xs text-[var(--brand-text-muted)] uppercase tracking-wide">Total Print Hours</div>
        </div>
        <div className="bg-purple-900/30 rounded-md p-4 text-center border border-purple-700/30">
          <Printer size={18} className="mx-auto mb-1 text-purple-400" />
          <div className="text-2xl font-bold font-display tabular-nums text-purple-400">{fleetJobs}</div>
          <div className="text-xs text-[var(--brand-text-muted)] uppercase tracking-wide">Jobs Completed</div>
        </div>
        <div className="bg-green-900/30 rounded-md p-4 text-center border border-green-700/30">
          <CheckCircle size={18} className="mx-auto mb-1 text-green-400" />
          <div className="text-2xl font-bold font-display tabular-nums text-green-400">{fleetSuccessRate}%</div>
          <div className="text-xs text-[var(--brand-text-muted)] uppercase tracking-wide">Success Rate</div>
        </div>
        <div className="bg-red-900/30 rounded-md p-4 text-center border border-red-700/30">
          <XCircle size={18} className="mx-auto mb-1 text-red-400" />
          <div className="text-2xl font-bold font-display tabular-nums text-red-400">{fleetFailed}</div>
          <div className="text-xs text-[var(--brand-text-muted)] uppercase tracking-wide">Failed Prints</div>
        </div>
      </div>

      {/* Charts Row */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Utilization by Printer */}
        <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
          <h3 className="text-sm font-semibold text-[var(--brand-text-secondary)] mb-4">Utilization by Printer</h3>
          {utilData.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <BarChart data={utilData} layout="vertical" margin={{ left: 10 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1A1D25" />
                <XAxis type="number" domain={[0, 100]} tickFormatter={v => v + '%'} tick={{ fill: '#3D4559', fontSize: 11 }} />
                <YAxis type="category" dataKey="name" width={100} tick={{ fill: '#3D4559', fontSize: 11 }} />
                <Tooltip contentStyle={CHART_TOOLTIP}
                  formatter={(v, name) => [name === 'utilization' ? v + '%' : v + 'h', name === 'utilization' ? 'Utilization' : 'Hours']} />
                <Bar dataKey="utilization" fill="#60a5fa" radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : <div className="text-[var(--brand-text-muted)] text-sm text-center py-12">No printer data available</div>}
        </div>

        {/* Jobs per Day */}
        <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
          <h3 className="text-sm font-semibold text-[var(--brand-text-secondary)] mb-4">Jobs Completed per Day</h3>
          {dailyData.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <BarChart data={dailyData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1A1D25" />
                <XAxis dataKey="date" tick={{ fill: '#3D4559', fontSize: 11 }} />
                <YAxis tick={{ fill: '#3D4559', fontSize: 11 }} allowDecimals={false} />
                <Tooltip contentStyle={CHART_TOOLTIP} />
                <Bar dataKey="jobs" fill="#34d399" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : <div className="text-[var(--brand-text-muted)] text-sm text-center py-12">No job data available</div>}
        </div>
      </div>

      {/* Success/Failure Pie + Per-Printer Table */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Pie */}
        <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
          <h3 className="text-sm font-semibold text-[var(--brand-text-secondary)] mb-4">Success vs Failure</h3>
          {pieData.length > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
              <PieChart>
                <Pie data={pieData} dataKey="value" cx="50%" cy="50%" outerRadius={80} label={({ name, value }) => `${name}: ${value}`}>
                  {pieData.map((entry, i) => <Cell key={i} fill={entry.color} />)}
                </Pie>
                <Tooltip contentStyle={CHART_TOOLTIP} />
              </PieChart>
            </ResponsiveContainer>
          ) : <div className="text-[var(--brand-text-muted)] text-sm text-center py-12">No data</div>}
        </div>

        {/* Per-Printer Table */}
        <div className="lg:col-span-2 bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 overflow-x-auto">
          <h3 className="text-sm font-semibold text-[var(--brand-text-secondary)] mb-4">Per-Printer Breakdown</h3>
          <table className="w-full text-sm">
            <thead>
              <tr className="text-[var(--brand-text-muted)] text-xs uppercase tracking-wide border-b border-[var(--brand-card-border)]">
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
                <tr key={p.id} className="border-b border-[var(--brand-card-border)]/50 hover:bg-[var(--brand-input-bg)]/30">
                  <td className="py-2 px-2 text-[var(--brand-text-primary)] font-medium">{p.name}</td>
                  <td className="py-2 px-2 text-right tabular-nums text-[var(--brand-text-secondary)]">{p.completed_jobs}</td>
                  <td className="py-2 px-2 text-right tabular-nums text-[var(--brand-text-secondary)]">{p.total_hours}h</td>
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
                  <td className="py-2 px-2 text-right tabular-nums text-[var(--brand-text-muted)]">{p.avg_job_hours}h</td>
                </tr>
              ))}
              {printerStats.length === 0 && (
                <tr><td colSpan={6} className="text-center py-8 text-[var(--brand-text-muted)]">No printer utilization data yet</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
