import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { educationReports } from '../../api'
import {
  Users, Clock, FileText, CheckCircle, XCircle,
  Download, BarChart3, ChevronUp, ChevronDown
} from 'lucide-react'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, PieChart, Pie, Cell,
  AreaChart, Area
} from 'recharts'
import clsx from 'clsx'

const metric = (value, suffix = '') => typeof value === 'number' && Number.isFinite(value) ? `${value}${suffix}` : 'Unknown'

const knownMetric = (value, unknown, suffix) => typeof value !== 'number' || !Number.isFinite(value) ? 'Unknown' : unknown > 0 ? (value === 0 ? 'Unknown' : `${metric(value, suffix)} known (${unknown} unknown)`) : metric(value, suffix)

// Job-estimate cost in the org's current currency; never a unitless amount.
const estimatedCost = (row) => {
  if (typeof row?.total_cost !== 'number' || !Number.isFinite(row.total_cost)) return 'Unknown'
  if (typeof row.currency !== 'string' || !row.currency) return 'Unknown (currency not set)'
  const amount = `Est. ${row.currency} ${row.total_cost.toFixed(row.currency === 'JPY' ? 0 : 2)}`
  return row.unknown_cost_jobs > 0 ? `${amount} (${row.unknown_cost_jobs} unpriced)` : amount
}

const CHART_TOOLTIP = { backgroundColor: 'var(--chart-tooltip-bg)', border: 'none', borderRadius: '6px', boxShadow: 'var(--chart-tooltip-shadow)', color: 'var(--brand-text-primary)' }

export default function EducationReports() {
  const [days, setDays] = useState(30)
  const [sortField, setSortField] = useState('total_jobs_submitted')
  const [sortDir, setSortDir] = useState('desc')

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ['education-usage-report', days],
    queryFn: () => educationReports.getUsageReport(days),
  })

  const summary: Partial<NonNullable<typeof data>['summary']> = data?.summary || {}
  const users = data?.users || []
  const dailySubmissions = data?.daily_submissions || {}

  // Sort users
  const sortedUsers = useMemo(() => {
    return [...users].sort((a, b) => {
      const aVal = a[sortField] ?? 0
      const bVal = b[sortField] ?? 0
      if (typeof aVal === 'string') return sortDir === 'asc' ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal)
      return sortDir === 'asc' ? aVal - bVal : bVal - aVal
    })
  }, [users, sortField, sortDir])

  // Top 10 users for bar chart
  const topUsersData = users.slice(0, 10).map(u => ({
    name: u.username.length > 14 ? u.username.slice(0, 14) + '...' : u.username,
    jobs: u.total_jobs_submitted,
  }))

  // Approval pie data
  const totalApproved = users.reduce((s, u) => s + u.total_jobs_approved, 0)
  const totalRejected = users.reduce((s, u) => s + u.total_jobs_rejected, 0)
  const pieData = [
    { name: 'Approved', value: totalApproved, color: '#34d399' },
    { name: 'Rejected', value: totalRejected, color: '#f87171' },
  ].filter(d => d.value > 0)

  // Daily submissions for area chart
  const dailyData = Object.entries(dailySubmissions)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, count]) => ({ date: date.slice(5), submissions: count }))

  const handleSort = (field) => {
    if (sortField === field) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortField(field)
      setSortDir('desc')
    }
  }

  const SortIcon = ({ field }) => {
    if (sortField !== field) return null
    return sortDir === 'asc' ? <ChevronUp size={12} className="inline ml-0.5" /> : <ChevronDown size={12} className="inline ml-0.5" />
  }

  const exportCSV = () => {
    if (!users.length) return
    const rows = [['Username', 'Email', 'Role', 'Jobs Submitted', 'Approved', 'Rejected', 'Completed', 'Failed', 'Actual Print Hours', 'Estimated Filament (g)', 'Time Accuracy %', 'Cost', 'Approval Rate %', 'Success Rate %', 'Last Activity', 'Unknown Actual Hours Jobs', 'Unknown Filament Estimate Jobs', 'Accuracy Samples', 'Unknown Accuracy Jobs', 'Hours Unavailable Reason', 'Filament Unavailable Reason']]
    users.forEach(u => {
      rows.push([u.username, u.email, u.role, u.total_jobs_submitted, u.total_jobs_approved, u.total_jobs_rejected, u.total_jobs_completed, u.total_jobs_failed, u.total_print_hours ?? '', u.estimated_filament_grams ?? '', u.accuracy ?? '', estimatedCost(u), u.approval_rate, u.success_rate, u.last_activity || '', u.unknown_actual_hours_jobs ?? '', u.unknown_filament_estimate_jobs ?? '', u.accuracy_samples ?? '', u.unknown_accuracy_jobs ?? '', u.total_print_hours_unavailable_reason || '', u.estimated_filament_grams_unavailable_reason || ''])
    })
    const cell = value => {
      let text = String(value ?? '')
      if (/^[\s]*[=+\-@]/.test(text) || /^[\t\r\n]/.test(text)) text = "'" + text
      return '"' + text.replace(/"/g, '""') + '"'
    }
    const csv = rows.map(r => r.map(cell).join(',')).join('\r\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `education-usage-report-${days}d-${new Date().toISOString().split('T')[0]}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  if (isError) return <div role="alert" className="p-6">Usage report could not be loaded. Try a shorter time window.<button onClick={() => refetch()} className="ml-3 underline">Retry</button></div>

  if (isLoading) return <div className="text-center py-12 text-[var(--brand-text-muted)]">Loading usage report...</div>

  return (
    <div className="p-4 md:p-6 space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div className="flex items-center gap-3">
          <BarChart3 className="text-[var(--brand-primary)]" size={24} />
          <div>
            <h1 className="text-xl md:text-2xl font-display font-bold">Usage Reports</h1>
            <p className="text-sm text-[var(--brand-text-muted)]">Student and user print activity</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {[7, 14, 30, 90].map(d => (
            <button key={d} onClick={() => setDays(d)}
              className={clsx('px-3 py-1.5 rounded-md text-sm transition-colors',
                days === d ? 'bg-[var(--brand-text-primary)] text-[var(--brand-content-bg)] font-medium' : 'bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)] hover:bg-[var(--brand-card-border)]'
              )}>
              {d}d
            </button>
          ))}
          <button onClick={exportCSV} className="flex items-center gap-1.5 px-3 py-1.5 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md text-sm text-[var(--brand-text-secondary)] transition-colors">
            <Download size={14} /> Export CSV
          </button>
        </div>
      </div>

      <p className="text-sm text-[var(--brand-text-muted)]">Hours show completed print time. Filament is estimated from model data. Time accuracy compares retained schedules with completed print time. Costs are job estimates shown in your organization's current currency.</p>
      {summary.unproven_history_jobs > 0 && <p className="text-sm text-[var(--brand-text-muted)]">Older records without proven ownership contribute activity counts only.</p>}
      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 md:gap-3">
        <div className="bg-[var(--brand-card-bg)] rounded-md p-4 text-center border border-[var(--brand-card-border)]">
          <Users size={18} className="mx-auto mb-1 text-[var(--brand-text-primary)]" />
          <div className="text-2xl font-bold tabular-nums text-[var(--brand-text-primary)]">{summary.total_users_active || 0}</div>
          <div className="text-xs text-[var(--brand-text-muted)] uppercase tracking-wide">Active Users</div>
        </div>
        <div className="bg-[var(--brand-card-bg)] rounded-md p-4 text-center border border-[var(--brand-card-border)]">
          <Clock size={18} className="mx-auto mb-1 text-[var(--brand-text-primary)]" />
          <div className="text-2xl font-bold tabular-nums text-[var(--brand-text-primary)]">{knownMetric(summary.total_print_hours, summary.unknown_actual_hours_jobs, 'h')}</div>
          <div className="text-xs text-[var(--brand-text-muted)] uppercase tracking-wide">Actual Print Hours</div>
        </div>
        <div className="bg-[var(--brand-card-bg)] rounded-md p-4 text-center border border-[var(--brand-card-border)]">
          <FileText size={18} className="mx-auto mb-1 text-[var(--brand-text-primary)]" />
          <div className="text-2xl font-bold tabular-nums text-[var(--brand-text-primary)]">{summary.total_jobs || 0}</div>
          <div className="text-xs text-[var(--brand-text-muted)] uppercase tracking-wide">Total Jobs</div>
        </div>
        <div className="bg-[var(--brand-card-bg)] rounded-md p-4 text-center border border-[var(--brand-card-border)]">
          <CheckCircle size={18} className="mx-auto mb-1 text-[var(--brand-text-primary)]" />
          <div className="text-2xl font-bold tabular-nums text-[var(--brand-text-primary)]">{summary.approval_rate || 0}%</div>
          <div className="text-xs text-[var(--brand-text-muted)] uppercase tracking-wide">Approval Rate</div>
        </div>
        <div className="bg-[var(--brand-card-bg)] rounded-md p-4 text-center border border-[var(--brand-card-border)]">
          <XCircle size={18} className="mx-auto mb-1 text-[var(--brand-text-primary)]" />
          <div className="text-2xl font-bold tabular-nums text-[var(--brand-text-primary)]">{summary.rejection_rate || 0}%</div>
          <div className="text-xs text-[var(--brand-text-muted)] uppercase tracking-wide">Rejection Rate</div>
        </div>
      </div>

      {/* Charts Row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Jobs per User */}
        <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
          <h3 className="text-sm font-semibold text-[var(--brand-text-secondary)] mb-4">Jobs per User (Top 10)</h3>
          {topUsersData.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <BarChart data={topUsersData} layout="vertical" margin={{ left: 10 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1A1D25" />
                <XAxis type="number" tick={{ fill: '#3D4559', fontSize: 11 }} allowDecimals={false} />
                <YAxis type="category" dataKey="name" width={100} tick={{ fill: '#3D4559', fontSize: 11 }} />
                <Tooltip contentStyle={CHART_TOOLTIP} />
                <Bar dataKey="jobs" fill="#60a5fa" radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : <div className="text-[var(--brand-text-muted)] text-sm text-center py-12">No user data available</div>}
        </div>

        {/* Approval vs Rejection Pie */}
        <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
          <h3 className="text-sm font-semibold text-[var(--brand-text-secondary)] mb-4">Approval vs Rejection</h3>
          {pieData.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <PieChart>
                <Pie data={pieData} dataKey="value" cx="50%" cy="50%" outerRadius={80}
                  label={({ name, value }) => `${name}: ${value}`}>
                  {pieData.map((entry, i) => <Cell key={i} fill={entry.color} />)}
                </Pie>
                <Tooltip contentStyle={CHART_TOOLTIP} />
              </PieChart>
            </ResponsiveContainer>
          ) : <div className="text-[var(--brand-text-muted)] text-sm text-center py-12">No approval data</div>}
        </div>

        {/* Daily Submissions Area Chart */}
        <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4">
          <h3 className="text-sm font-semibold text-[var(--brand-text-secondary)] mb-4">Daily Job Submissions</h3>
          {dailyData.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <AreaChart data={dailyData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1A1D25" />
                <XAxis dataKey="date" tick={{ fill: '#3D4559', fontSize: 11 }} />
                <YAxis tick={{ fill: '#3D4559', fontSize: 11 }} allowDecimals={false} />
                <Tooltip contentStyle={CHART_TOOLTIP} />
                <Area type="monotone" dataKey="submissions" stroke="#3B82F6" fill="#3B82F6" fillOpacity={0.1} strokeWidth={2} dot={false} />
              </AreaChart>
            </ResponsiveContainer>
          ) : <div className="text-[var(--brand-text-muted)] text-sm text-center py-12">No submission data</div>}
        </div>
      </div>

      {(['printer', 'model'] as const).map(kind => (
        <div key={kind} role="region" aria-label={`Usage by ${kind} table`} tabIndex={0} className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 overflow-x-auto">
          <h3 className="text-sm font-semibold mb-3">Usage by {kind === 'printer' ? 'Printer' : 'Model'}</h3>
          <table className="w-full text-sm"><thead><tr><th className="text-left">{kind === 'printer' ? 'Printer' : 'Model'}</th><th>Jobs</th><th>Actual Hours</th><th>Time Accuracy</th><th>Cost</th></tr></thead>
            <tbody>{(data?.[`by_${kind}`] || []).map((group, index) => <tr key={group.id ?? `unavailable-${index}`}>
              <td>{group.available && group.name ? group.name : 'Unavailable resource'}</td><td className="text-center">{group.total_jobs_submitted}</td>
              <td className="text-center">{knownMetric(group.total_print_hours, group.unknown_actual_hours_jobs, 'h')}</td><td className="text-center">{metric(group.accuracy, '%')}</td>
              <td className="text-center">{estimatedCost(group)}</td>
            </tr>)}</tbody></table>
        </div>
      ))}
      {/* User Table */}
      <div role="region" aria-label="Usage by user table" tabIndex={0} className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] p-4 overflow-x-auto">
        <h3 className="text-sm font-semibold text-[var(--brand-text-secondary)] mb-4">Usage by User</h3>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-[var(--brand-text-muted)] text-xs uppercase tracking-wide border-b border-[var(--brand-card-border)]">
              <th className="text-left py-2 px-2" aria-sort={sortField === 'username' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}><button type="button" onClick={() => handleSort('username')} className="min-h-[44px] px-1 rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--brand-primary)]">User <SortIcon field="username" /></button></th>
              <th className="text-right py-2 px-2" aria-sort={sortField === 'total_jobs_submitted' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}><button type="button" onClick={() => handleSort('total_jobs_submitted')} className="min-h-[44px] px-1 rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--brand-primary)]">Submitted <SortIcon field="total_jobs_submitted" /></button></th>
              <th className="text-right py-2 px-2 hidden md:table-cell" aria-sort={sortField === 'total_jobs_approved' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}><button type="button" onClick={() => handleSort('total_jobs_approved')} className="min-h-[44px] px-1 rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--brand-primary)]">Approved <SortIcon field="total_jobs_approved" /></button></th>
              <th className="text-right py-2 px-2 hidden md:table-cell" aria-sort={sortField === 'total_jobs_rejected' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}><button type="button" onClick={() => handleSort('total_jobs_rejected')} className="min-h-[44px] px-1 rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--brand-primary)]">Rejected <SortIcon field="total_jobs_rejected" /></button></th>
              <th className="text-right py-2 px-2" aria-sort={sortField === 'total_jobs_completed' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}><button type="button" onClick={() => handleSort('total_jobs_completed')} className="min-h-[44px] px-1 rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--brand-primary)]">Completed <SortIcon field="total_jobs_completed" /></button></th>
              <th className="text-right py-2 px-2 hidden md:table-cell" aria-sort={sortField === 'total_print_hours' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}><button type="button" onClick={() => handleSort('total_print_hours')} className="min-h-[44px] px-1 rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--brand-primary)]">Hours <SortIcon field="total_print_hours" /></button></th>
              <th className="text-right py-2 px-2 hidden lg:table-cell" aria-sort={sortField === 'estimated_filament_grams' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}><button type="button" onClick={() => handleSort('estimated_filament_grams')} className="min-h-[44px] px-1 rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--brand-primary)]">Estimated Filament <SortIcon field="estimated_filament_grams" /></button></th>
              <th className="text-right py-2 px-2" aria-sort={sortField === 'approval_rate' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}><button type="button" onClick={() => handleSort('approval_rate')} className="min-h-[44px] px-1 rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--brand-primary)]">Approval % <SortIcon field="approval_rate" /></button></th>
              <th className="text-right py-2 px-2 hidden md:table-cell" aria-sort={sortField === 'success_rate' ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}><button type="button" onClick={() => handleSort('success_rate')} className="min-h-[44px] px-1 rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--brand-primary)]">Success % <SortIcon field="success_rate" /></button></th>
              <th className="text-right py-2 px-2">Time Accuracy</th>
              <th className="text-right py-2 px-2">Cost</th>
              <th className="text-right py-2 px-2 hidden lg:table-cell">Last Active</th>
            </tr>
          </thead>
          <tbody>
            {sortedUsers.map(u => (
              <tr key={u.user_id} className="border-b border-[var(--brand-card-border)]/50 hover:bg-[var(--brand-input-bg)]/30">
                <td className="py-2 px-2">
                  <div className="text-[var(--brand-text-primary)] font-medium">{u.username}</div>
                  <div className="text-xs text-[var(--brand-text-muted)]">{u.email}</div>
                </td>
                <td className="py-2 px-2 text-right tabular-nums text-[var(--brand-text-secondary)]">{u.total_jobs_submitted}</td>
                <td className="py-2 px-2 text-right tabular-nums text-[var(--brand-text-primary)] hidden md:table-cell">{u.total_jobs_approved}</td>
                <td className="py-2 px-2 text-right tabular-nums text-[var(--brand-text-primary)] hidden md:table-cell">{u.total_jobs_rejected}</td>
                <td className="py-2 px-2 text-right tabular-nums text-[var(--brand-text-secondary)]">{u.total_jobs_completed}</td>
                <td className="py-2 px-2 text-right tabular-nums text-[var(--brand-text-secondary)] hidden md:table-cell">{knownMetric(u.total_print_hours, u.unknown_actual_hours_jobs, 'h')}</td>
                <td className="py-2 px-2 text-right tabular-nums text-[var(--brand-text-secondary)] hidden lg:table-cell">{knownMetric(u.estimated_filament_grams, u.unknown_filament_estimate_jobs, 'g')}</td>
                <td className="py-2 px-2 text-right tabular-nums">
                  <span className={clsx('font-medium', u.approval_rate >= 80 ? 'text-[var(--brand-text-primary)]' : u.approval_rate >= 50 ? 'text-[var(--brand-text-primary)]' : 'text-[var(--brand-text-primary)]')}>
                    {u.approval_rate}%
                  </span>
                </td>
                <td className="py-2 px-2 text-right tabular-nums hidden md:table-cell">
                  <span className={clsx('font-medium', u.success_rate >= 90 ? 'text-[var(--brand-text-primary)]' : u.success_rate >= 70 ? 'text-[var(--brand-text-primary)]' : 'text-[var(--brand-text-primary)]')}>
                    {u.success_rate}%
                  </span>
                </td>
                <td className="py-2 px-2 text-right">{metric(u.accuracy, '%')}</td>
                <td className="py-2 px-2 text-right">{estimatedCost(u)}</td>
                <td className="py-2 px-2 text-right text-[var(--brand-text-muted)] text-xs hidden lg:table-cell">
                  {u.last_activity ? new Date(u.last_activity).toLocaleDateString() : 'Unknown'}
                </td>
              </tr>
            ))}
            {sortedUsers.length === 0 && (
              <tr><td colSpan={12} className="text-center py-8 text-[var(--brand-text-muted)]">No user activity in the last {days} days</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
