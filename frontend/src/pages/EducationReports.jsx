import { useState, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { educationReports } from '../api'
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

export default function EducationReports() {
  const [days, setDays] = useState(30)
  const [sortField, setSortField] = useState('total_jobs_submitted')
  const [sortDir, setSortDir] = useState('desc')

  const { data, isLoading } = useQuery({
    queryKey: ['education-usage-report', days],
    queryFn: () => educationReports.getUsageReport(days),
  })

  const summary = data?.summary || {}
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
    const rows = [['Username', 'Email', 'Role', 'Jobs Submitted', 'Approved', 'Rejected', 'Completed', 'Failed', 'Print Hours', 'Filament (g)', 'Approval Rate %', 'Success Rate %', 'Last Activity']]
    users.forEach(u => {
      rows.push([u.username, u.email, u.role, u.total_jobs_submitted, u.total_jobs_approved, u.total_jobs_rejected, u.total_jobs_completed, u.total_jobs_failed, u.total_print_hours, u.total_filament_grams, u.approval_rate, u.success_rate, u.last_activity || ''])
    })
    const csv = rows.map(r => r.join(',')).join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `education-usage-report-${days}d-${new Date().toISOString().split('T')[0]}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  if (isLoading) return <div className="text-center py-12 text-farm-500">Loading usage report...</div>

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div className="flex items-center gap-3">
          <BarChart3 className="text-print-400" size={24} />
          <div>
            <h1 className="text-xl font-bold text-farm-100">Usage Reports</h1>
            <p className="text-sm text-farm-500">Student and user print activity</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {[7, 14, 30, 90].map(d => (
            <button key={d} onClick={() => setDays(d)}
              className={clsx('px-3 py-1.5 rounded-lg text-sm transition-colors',
                days === d ? 'bg-amber-500 text-farm-950 font-medium' : 'bg-farm-800 text-farm-300 hover:bg-farm-700'
              )}>
              {d}d
            </button>
          ))}
          <button onClick={exportCSV} className="flex items-center gap-1.5 px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm text-farm-300 transition-colors">
            <Download size={14} /> Export CSV
          </button>
        </div>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 md:gap-3">
        <div className="bg-blue-400/10 rounded-lg p-4 text-center border border-farm-800">
          <Users size={18} className="mx-auto mb-1 text-blue-400" />
          <div className="text-2xl font-bold tabular-nums text-blue-400">{summary.total_users_active || 0}</div>
          <div className="text-xs text-farm-500 uppercase tracking-wide">Active Users</div>
        </div>
        <div className="bg-emerald-400/10 rounded-lg p-4 text-center border border-farm-800">
          <Clock size={18} className="mx-auto mb-1 text-emerald-400" />
          <div className="text-2xl font-bold tabular-nums text-emerald-400">{summary.total_print_hours || 0}h</div>
          <div className="text-xs text-farm-500 uppercase tracking-wide">Print Hours</div>
        </div>
        <div className="bg-purple-400/10 rounded-lg p-4 text-center border border-farm-800">
          <FileText size={18} className="mx-auto mb-1 text-purple-400" />
          <div className="text-2xl font-bold tabular-nums text-purple-400">{summary.total_jobs || 0}</div>
          <div className="text-xs text-farm-500 uppercase tracking-wide">Total Jobs</div>
        </div>
        <div className="bg-green-400/10 rounded-lg p-4 text-center border border-farm-800">
          <CheckCircle size={18} className="mx-auto mb-1 text-green-400" />
          <div className="text-2xl font-bold tabular-nums text-green-400">{summary.approval_rate || 0}%</div>
          <div className="text-xs text-farm-500 uppercase tracking-wide">Approval Rate</div>
        </div>
        <div className="bg-red-400/10 rounded-lg p-4 text-center border border-farm-800">
          <XCircle size={18} className="mx-auto mb-1 text-red-400" />
          <div className="text-2xl font-bold tabular-nums text-red-400">{summary.rejection_rate || 0}%</div>
          <div className="text-xs text-farm-500 uppercase tracking-wide">Rejection Rate</div>
        </div>
      </div>

      {/* Charts Row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Jobs per User */}
        <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
          <h3 className="text-sm font-semibold text-farm-300 mb-4">Jobs per User (Top 10)</h3>
          {topUsersData.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <BarChart data={topUsersData} layout="vertical" margin={{ left: 10 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
                <XAxis type="number" stroke="var(--chart-axis)" fontSize={11} allowDecimals={false} />
                <YAxis type="category" dataKey="name" width={100} stroke="var(--chart-axis)" fontSize={11} />
                <Tooltip contentStyle={{ background: 'var(--chart-tooltip-bg)', border: '1px solid var(--chart-tooltip-border)', borderRadius: 8, fontSize: 12 }} />
                <Bar dataKey="jobs" fill="#60a5fa" radius={[0, 4, 4, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : <div className="text-farm-600 text-sm text-center py-12">No user data available</div>}
        </div>

        {/* Approval vs Rejection Pie */}
        <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
          <h3 className="text-sm font-semibold text-farm-300 mb-4">Approval vs Rejection</h3>
          {pieData.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <PieChart>
                <Pie data={pieData} dataKey="value" cx="50%" cy="50%" outerRadius={80}
                  label={({ name, value }) => `${name}: ${value}`}>
                  {pieData.map((entry, i) => <Cell key={i} fill={entry.color} />)}
                </Pie>
                <Tooltip />
              </PieChart>
            </ResponsiveContainer>
          ) : <div className="text-farm-600 text-sm text-center py-12">No approval data</div>}
        </div>

        {/* Daily Submissions Area Chart */}
        <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
          <h3 className="text-sm font-semibold text-farm-300 mb-4">Daily Job Submissions</h3>
          {dailyData.length > 0 ? (
            <ResponsiveContainer width="100%" height={250}>
              <AreaChart data={dailyData}>
                <defs>
                  <linearGradient id="gradSubmissions" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#3B82F6" stopOpacity={0.3} />
                    <stop offset="95%" stopColor="#3B82F6" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--chart-grid)" />
                <XAxis dataKey="date" stroke="var(--chart-axis)" fontSize={10} />
                <YAxis stroke="var(--chart-axis)" fontSize={10} allowDecimals={false} />
                <Tooltip contentStyle={{ background: 'var(--chart-tooltip-bg)', border: '1px solid var(--chart-tooltip-border)', borderRadius: 8, fontSize: 12 }} />
                <Area type="monotone" dataKey="submissions" stroke="#3B82F6" fill="url(#gradSubmissions)" strokeWidth={2} dot={false} />
              </AreaChart>
            </ResponsiveContainer>
          ) : <div className="text-farm-600 text-sm text-center py-12">No submission data</div>}
        </div>
      </div>

      {/* User Table */}
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 overflow-x-auto">
        <h3 className="text-sm font-semibold text-farm-300 mb-4">Usage by User</h3>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-farm-500 text-xs uppercase tracking-wide border-b border-farm-800">
              <th className="text-left py-2 px-2 cursor-pointer" onClick={() => handleSort('username')}>User <SortIcon field="username" /></th>
              <th className="text-right py-2 px-2 cursor-pointer" onClick={() => handleSort('total_jobs_submitted')}>Submitted <SortIcon field="total_jobs_submitted" /></th>
              <th className="text-right py-2 px-2 cursor-pointer hidden md:table-cell" onClick={() => handleSort('total_jobs_approved')}>Approved <SortIcon field="total_jobs_approved" /></th>
              <th className="text-right py-2 px-2 cursor-pointer hidden md:table-cell" onClick={() => handleSort('total_jobs_rejected')}>Rejected <SortIcon field="total_jobs_rejected" /></th>
              <th className="text-right py-2 px-2 cursor-pointer" onClick={() => handleSort('total_jobs_completed')}>Completed <SortIcon field="total_jobs_completed" /></th>
              <th className="text-right py-2 px-2 cursor-pointer hidden md:table-cell" onClick={() => handleSort('total_print_hours')}>Hours <SortIcon field="total_print_hours" /></th>
              <th className="text-right py-2 px-2 cursor-pointer hidden lg:table-cell" onClick={() => handleSort('total_filament_grams')}>Filament <SortIcon field="total_filament_grams" /></th>
              <th className="text-right py-2 px-2 cursor-pointer" onClick={() => handleSort('approval_rate')}>Approval % <SortIcon field="approval_rate" /></th>
              <th className="text-right py-2 px-2 cursor-pointer hidden md:table-cell" onClick={() => handleSort('success_rate')}>Success % <SortIcon field="success_rate" /></th>
              <th className="text-right py-2 px-2 hidden lg:table-cell">Last Active</th>
            </tr>
          </thead>
          <tbody>
            {sortedUsers.map(u => (
              <tr key={u.user_id} className="border-b border-farm-800/50 hover:bg-farm-800/30">
                <td className="py-2 px-2">
                  <div className="text-farm-200 font-medium">{u.username}</div>
                  <div className="text-xs text-farm-500">{u.email}</div>
                </td>
                <td className="py-2 px-2 text-right tabular-nums text-farm-300">{u.total_jobs_submitted}</td>
                <td className="py-2 px-2 text-right tabular-nums text-emerald-400 hidden md:table-cell">{u.total_jobs_approved}</td>
                <td className="py-2 px-2 text-right tabular-nums text-red-400 hidden md:table-cell">{u.total_jobs_rejected}</td>
                <td className="py-2 px-2 text-right tabular-nums text-farm-300">{u.total_jobs_completed}</td>
                <td className="py-2 px-2 text-right tabular-nums text-farm-300 hidden md:table-cell">{u.total_print_hours}h</td>
                <td className="py-2 px-2 text-right tabular-nums text-farm-300 hidden lg:table-cell">{u.total_filament_grams}g</td>
                <td className="py-2 px-2 text-right tabular-nums">
                  <span className={clsx('font-medium', u.approval_rate >= 80 ? 'text-emerald-400' : u.approval_rate >= 50 ? 'text-yellow-400' : 'text-red-400')}>
                    {u.approval_rate}%
                  </span>
                </td>
                <td className="py-2 px-2 text-right tabular-nums hidden md:table-cell">
                  <span className={clsx('font-medium', u.success_rate >= 90 ? 'text-emerald-400' : u.success_rate >= 70 ? 'text-yellow-400' : 'text-red-400')}>
                    {u.success_rate}%
                  </span>
                </td>
                <td className="py-2 px-2 text-right text-farm-500 text-xs hidden lg:table-cell">
                  {u.last_activity ? new Date(u.last_activity).toLocaleDateString() : '—'}
                </td>
              </tr>
            ))}
            {sortedUsers.length === 0 && (
              <tr><td colSpan={10} className="text-center py-8 text-farm-600">No user activity in the last {days} days</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
