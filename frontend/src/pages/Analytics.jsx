import { useQuery } from '@tanstack/react-query'
import { analytics, printJobs } from '../api'
import { useState } from 'react'
import {
  TrendingUp, TrendingDown, DollarSign, Clock, Printer,
  BarChart3, Target, Activity, CheckCircle, XCircle,
} from 'lucide-react'
import {
  AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, Legend,
} from 'recharts'
import EnergyWidget from '../components/EnergyWidget'

const COLORS = ['#3B82F6', '#22C55E', '#F59E0B', '#EF4444', '#8B5CF6', '#06B6D4', '#EC4899', '#F97316']

function HeroStat({ title, value, subtitle, icon: Icon, color }) {
  const colorMap = {
    green: 'bg-green-900/30 text-green-400 border-green-700/30',
    yellow: 'bg-amber-900/30 text-amber-400 border-amber-700/30',
    blue: 'bg-blue-900/30 text-blue-400 border-blue-700/30',
    purple: 'bg-purple-900/30 text-purple-400 border-purple-700/30',
    red: 'bg-red-900/30 text-red-400 border-red-700/30',
    orange: 'bg-orange-900/30 text-orange-400 border-orange-700/30',
    cyan: 'bg-cyan-900/30 text-cyan-400 border-cyan-700/30',
    default: 'bg-farm-900 text-farm-100 border-farm-800',
  }
  const textColorMap = {
    green: 'text-green-400', yellow: 'text-amber-400', blue: 'text-blue-400',
    purple: 'text-purple-400', red: 'text-red-400', orange: 'text-orange-400',
    cyan: 'text-cyan-400', default: 'text-farm-100',
  }
  const classes = colorMap[color] || colorMap.default
  const textColor = textColorMap[color] || textColorMap.default

  return (
    <div className={`rounded-lg p-3 md:p-4 border ${classes}`}>
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs text-farm-400 mb-1">{title}</p>
          <p className={`text-xl md:text-2xl font-bold font-display ${textColor}`}>{value}</p>
          {subtitle && <p className="text-xs text-farm-500 mt-1">{subtitle}</p>}
        </div>
        {Icon && (
          <div className="p-1.5 bg-farm-800/50 rounded-lg">
            <Icon size={20} className={textColor} />
          </div>
        )}
      </div>
    </div>
  )
}

function JobsOverTimeChart({ data }) {
  if (!data || Object.keys(data).length === 0) {
    return (
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
        <h3 className="text-sm font-semibold text-farm-300 mb-4">Job Activity</h3>
        <div className="text-center py-12 text-farm-600 text-sm">No job data yet</div>
      </div>
    )
  }

  const chartData = Object.entries(data)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, counts]) => ({
      date: new Date(date + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
      created: counts.created || 0,
      completed: counts.completed || 0,
    }))

  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-farm-300">Job Activity</h3>
        <span className="text-xs text-farm-500">Last 30 days</span>
      </div>
      <ResponsiveContainer width="100%" height={220}>
        <AreaChart data={chartData}>
          <defs>
            <linearGradient id="gradCreated" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#3B82F6" stopOpacity={0.3}/>
              <stop offset="95%" stopColor="#3B82F6" stopOpacity={0}/>
            </linearGradient>
            <linearGradient id="gradCompleted" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#22C55E" stopOpacity={0.3}/>
              <stop offset="95%" stopColor="#22C55E" stopOpacity={0}/>
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="#1F2937" />
          <XAxis dataKey="date" tick={{ fill: '#6B7280', fontSize: 10 }} axisLine={{ stroke: '#374151' }} tickLine={false} />
          <YAxis tick={{ fill: '#6B7280', fontSize: 10 }} axisLine={false} tickLine={false} allowDecimals={false} />
          <Tooltip
            contentStyle={{ background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
            labelStyle={{ color: '#6B7280', marginBottom: '4px' }}
          />
          <Legend wrapperStyle={{ fontSize: '11px', paddingTop: '8px' }} />
          <Area type="monotone" dataKey="created" stroke="#3B82F6" fill="url(#gradCreated)" strokeWidth={2} name="Created" dot={false} />
          <Area type="monotone" dataKey="completed" stroke="#22C55E" fill="url(#gradCompleted)" strokeWidth={2} name="Completed" dot={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

function PrinterUtilization({ data }) {
  const sorted = [...data].sort((a, b) => (b.utilization_pct || 0) - (a.utilization_pct || 0))

  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
      <h3 className="text-sm font-semibold text-farm-300 mb-4">Printer Utilization</h3>
      <div className="space-y-4">
        {sorted.map((printer) => {
          const pct = printer.utilization_pct || 0
          const barColor = pct >= 70 ? '#22C55E' : pct >= 40 ? '#EAB308' : pct > 0 ? '#F97316' : '#374151'
          const successColor = printer.success_rate >= 95 ? 'text-green-400' : printer.success_rate >= 80 ? 'text-yellow-400' : 'text-red-400'
          return (
            <div key={printer.id}>
              <div className="flex items-center justify-between mb-1.5">
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">{printer.name}</span>
                  <span className={`text-xs ${successColor}`}>
                    {printer.success_rate || 100}%
                  </span>
                </div>
                <span className="text-sm font-bold tabular-nums" style={{ color: barColor }}>{pct}%</span>
              </div>
              <div className="w-full bg-farm-800/50 rounded-full h-2.5">
                <div
                  className="h-2.5 rounded-full transition-all duration-500"
                  style={{ width: `${Math.min(pct, 100)}%`, backgroundColor: barColor }}
                />
              </div>
              <div className="flex gap-3 mt-1.5 text-xs text-farm-500">
                <span>{printer.completed_jobs} jobs</span>
                <span>{printer.total_hours}h total</span>
                <span>~{printer.avg_job_hours || 0}h/job</span>
                {printer.failed_jobs > 0 && (
                  <span className="text-red-400 flex items-center gap-0.5">
                    <XCircle size={10} />{printer.failed_jobs} failed
                  </span>
                )}
              </div>
            </div>
          )
        })}
        {data.length === 0 && (
          <div className="text-center text-farm-600 py-8 text-sm">No printer data yet</div>
        )}
      </div>
    </div>
  )
}

function ModelRankings({ topData, worstData }) {
  const RankRow = ({ item, index, isWorst }) => {
    const medalColors = isWorst
      ? ['bg-red-900/50 text-red-300', 'bg-red-900/30 text-red-400', 'bg-red-900/20 text-red-500']
      : ['bg-amber-900/50 text-amber-300', 'bg-amber-900/30 text-amber-400', 'bg-farm-700 text-farm-300']

    return (
      <div className="flex items-center gap-3 py-2.5 border-b border-farm-800/50 last:border-0">
        <span className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0 ${medalColors[Math.min(index, 2)]}`}>
          {index + 1}
        </span>
        <span className="text-sm flex-1 truncate">{item.name}</span>
        <div className="text-right flex-shrink-0">
          <div className={`font-semibold text-sm tabular-nums ${isWorst ? 'text-red-400' : 'text-green-400'}`}>
            ${item.value_per_hour?.toFixed(2)}/hr
          </div>
          <div className="text-xs text-farm-500 tabular-nums">
            ${item.value_per_bed?.toFixed(2)}/bed &middot; {item.build_time_hours}h
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
        <div className="flex items-center gap-2 mb-3">
          <TrendingUp size={16} className="text-green-400" />
          <h3 className="text-sm font-semibold text-farm-300">Top Performers</h3>
          <span className="text-xs text-farm-500">$/hour</span>
        </div>
        <div>
          {topData.map((item, i) => <RankRow key={item.id} item={item} index={i} />)}
          {topData.length === 0 && <div className="text-center text-farm-600 py-6 text-sm">No data yet</div>}
        </div>
      </div>
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
        <div className="flex items-center gap-2 mb-3">
          <TrendingDown size={16} className="text-red-400" />
          <h3 className="text-sm font-semibold text-farm-300">Needs Improvement</h3>
          <span className="text-xs text-farm-500">$/hour</span>
        </div>
        <div>
          {worstData.map((item, i) => <RankRow key={item.id} item={item} index={i} isWorst />)}
          {worstData.length === 0 && <div className="text-center text-farm-600 py-6 text-sm">No data yet</div>}
        </div>
      </div>
    </div>
  )
}

function FleetOverview({ summary }) {
  const stats = [
    { label: 'Total Jobs', value: summary.total_jobs, color: 'text-farm-100' },
    { label: 'Completed', value: summary.completed_jobs, color: 'text-green-400', icon: CheckCircle },
    { label: 'Pending', value: summary.pending_jobs, color: 'text-yellow-400', icon: Clock },
    { label: 'Models', value: summary.total_models, color: 'text-blue-400', icon: BarChart3 },
  ]

  const completionRate = summary.total_jobs > 0
    ? ((summary.completed_jobs / summary.total_jobs) * 100).toFixed(0)
    : 0

  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-farm-300">Fleet Overview</h3>
        <div className="flex items-center gap-1.5 text-xs">
          <div className="w-2 h-2 rounded-full bg-green-400" />
          <span className="text-farm-400">{completionRate}% completion rate</span>
        </div>
      </div>
      <div className="grid grid-cols-4 gap-3">
        {stats.map(s => (
          <div key={s.label} className="text-center p-3 rounded-lg bg-farm-800/40">
            <div className={`text-xl md:text-2xl font-bold font-display tabular-nums ${s.color}`}>{s.value}</div>
            <div className="text-xs text-farm-500 mt-0.5">{s.label}</div>
          </div>
        ))}
      </div>
    </div>
  )
}


function CostRevenueChart({ data }) {
  const chartData = data ? Object.entries(data)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, counts]) => ({
      date: new Date(date + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
      revenue: counts.revenue || 0,
      cost: counts.cost || 0,
      profit: (counts.revenue || 0) - (counts.cost || 0),
    }))
    .filter(d => d.revenue > 0 || d.cost > 0) : []

  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-farm-300">Revenue vs Cost</h3>
        <span className="text-xs text-farm-500">Last 30 days</span>
      </div>
      {chartData.length > 0 ? (
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={chartData}>
            <CartesianGrid strokeDasharray="3 3" stroke="#1F2937" />
            <XAxis dataKey="date" tick={{ fill: '#6B7280', fontSize: 10 }} axisLine={{ stroke: '#374151' }} tickLine={false} />
            <YAxis tick={{ fill: '#6B7280', fontSize: 10 }} axisLine={false} tickLine={false} tickFormatter={(v) => `$${v}`} />
            <Tooltip
              contentStyle={{ background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
              formatter={(value) => [`$${value.toFixed(2)}`, undefined]}
            />
            <Legend wrapperStyle={{ fontSize: '11px', paddingTop: '8px' }} />
            <Bar dataKey="revenue" fill="#22C55E" name="Revenue" radius={[3, 3, 0, 0]} />
            <Bar dataKey="cost" fill="#EF4444" name="Cost" radius={[3, 3, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      ) : (
        <div className="text-center py-12 text-farm-600 text-sm">No cost/revenue data yet</div>
      )}
    </div>
  )
}

function FailureRateByPrinterChart({ data }) {
  if (!data?.by_printer?.length) return null
  const chartData = data.by_printer.map(p => ({
    name: p.name,
    completed: p.completed,
    failed: p.failed,
  }))
  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-farm-300">Success / Failure by Printer</h3>
        <span className="text-xs text-farm-500">{data.overall_success_rate}% fleet success</span>
      </div>
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={chartData} barCategoryGap="20%">
          <CartesianGrid strokeDasharray="3 3" stroke="#1F2937" />
          <XAxis dataKey="name" tick={{ fill: '#6B7280', fontSize: 10 }} axisLine={false} tickLine={false} />
          <YAxis tick={{ fill: '#6B7280', fontSize: 10 }} axisLine={false} tickLine={false} />
          <Tooltip contentStyle={{ background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }} />
          <Legend wrapperStyle={{ fontSize: '11px', paddingTop: '8px' }} />
          <Bar dataKey="completed" stackId="a" fill="#22C55E" name="Completed" radius={[0, 0, 0, 0]} />
          <Bar dataKey="failed" stackId="a" fill="#EF4444" name="Failed" radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

function TopFailureReasons({ data }) {
  if (!data?.top_failure_reasons?.length) return null
  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
      <h3 className="text-sm font-semibold text-farm-300 mb-4">Top Failure Reasons</h3>
      <div className="space-y-2">
        {data.top_failure_reasons.map((r, i) => {
          const maxCount = data.top_failure_reasons[0]?.count || 1
          return (
            <div key={i} className="flex items-center gap-3">
              <span className="text-sm text-farm-400 w-32 truncate capitalize">{r.reason.replace(/_/g, ' ')}</span>
              <div className="flex-1 h-5 bg-farm-800 rounded-full overflow-hidden">
                <div className="h-full bg-red-500/60 rounded-full" style={{ width: `${(r.count / maxCount) * 100}%` }} />
              </div>
              <span className="text-sm text-farm-500 w-8 text-right">{r.count}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function TimeAccuracyChart({ data }) {
  if (!data || data.by_printer?.length === 0) {
    return (
      <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
        <h3 className="text-sm font-semibold text-farm-300 mb-4">Est vs Actual Print Time</h3>
        <div className="text-center py-12 text-farm-600 text-sm">Not enough completed jobs with timing data</div>
      </div>
    )
  }

  const chartData = data.by_printer.map(p => ({
    name: p.name,
    estimated: p.estimated_hours,
    actual: p.actual_hours,
  }))

  return (
    <div className="bg-farm-900 rounded-lg border border-farm-800 p-4">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-farm-300">Est vs Actual Print Time</h3>
        <span className="text-xs text-farm-500">{data.avg_accuracy_pct}% avg accuracy &middot; {data.total_jobs} jobs</span>
      </div>
      <ResponsiveContainer width="100%" height={220}>
        <BarChart data={chartData} barCategoryGap="20%">
          <CartesianGrid strokeDasharray="3 3" stroke="#1F2937" />
          <XAxis dataKey="name" tick={{ fill: '#6B7280', fontSize: 10 }} axisLine={false} tickLine={false} />
          <YAxis tick={{ fill: '#6B7280', fontSize: 10 }} axisLine={false} tickLine={false} tickFormatter={(v) => `${v}h`} />
          <Tooltip
            contentStyle={{ background: '#111827', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
            formatter={(value) => [`${value.toFixed(1)}h`, undefined]}
          />
          <Legend wrapperStyle={{ fontSize: '11px', paddingTop: '8px' }} />
          <Bar dataKey="estimated" fill="#3B82F6" name="Estimated" radius={[3, 3, 0, 0]} />
          <Bar dataKey="actual" fill="#22C55E" name="Actual" radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

const DATE_RANGES = [
  { label: '7d', value: 7 },
  { label: '14d', value: 14 },
  { label: '30d', value: 30 },
  { label: '90d', value: 90 },
]

export default function Analytics() {
  const [days, setDays] = useState(30)

  const { data, isLoading, error } = useQuery({
    queryKey: ['analytics', days],
    queryFn: () => analytics.get(days),
  })
  const { data: timeAccuracy, isLoading: timeAccuracyLoading } = useQuery({
    queryKey: ['time-accuracy', days],
    queryFn: () => analytics.timeAccuracy(days),
  })
  const { data: failureData, isLoading: failureLoading } = useQuery({
    queryKey: ['failure-analytics', days],
    queryFn: () => analytics.failures(days),
  })
  const { data: energyJobs, isLoading: energyLoading } = useQuery({
    queryKey: ['energy-jobs'],
    queryFn: () => printJobs.list({ limit: 200 }).catch(() => [])
  })
  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-farm-800 rounded-lg w-48"></div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {[...Array(4)].map((_, i) => (
              <div key={i} className="h-24 bg-farm-800/50 rounded-lg"></div>
            ))}
          </div>
          <div className="h-64 bg-farm-800/50 rounded-lg"></div>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="space-y-6">
        <div className="bg-red-900/30 border border-red-800 rounded-lg p-4 text-red-300 text-sm">
          Error loading analytics: {error.message}
        </div>
      </div>
    )
  }

  const { summary, top_by_hour, worst_performers, printer_stats, jobs_by_date } = data

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div className="flex items-center gap-3">
          <BarChart3 className="text-print-400" size={24} />
          <div>
            <h1 className="text-xl font-bold text-farm-100">Analytics</h1>
            <p className="text-sm text-farm-500">Revenue, profitability, and fleet performance</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={days}
            onChange={e => setDays(Number(e.target.value))}
            className="bg-farm-900 border border-farm-700 rounded-lg px-3 py-1.5 text-sm text-farm-300"
          >
            {DATE_RANGES.map(r => (
              <option key={r.value} value={r.value}>Last {r.label}</option>
            ))}
          </select>
        </div>
      </div>

      {/* Hero Stats */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <HeroStat
          title="Revenue"
          value={`$${summary.total_revenue.toFixed(2)}`}
          subtitle={`${summary.completed_jobs} completed jobs`}
          icon={DollarSign}
          color="green"
        />
        <HeroStat
          title="Pipeline"
          value={`$${summary.projected_revenue.toFixed(2)}`}
          subtitle={`${summary.pending_jobs} pending jobs`}
          icon={Target}
          color="yellow"
        />
        <HeroStat
          title="Avg $/Hour"
          value={`$${summary.avg_value_per_hour.toFixed(2)}`}
          subtitle="across all models"
          icon={TrendingUp}
          color="blue"
        />
        <HeroStat
          title="Print Hours"
          value={summary.total_print_hours}
          subtitle={`${summary.total_models} models`}
          icon={Clock}
          color="purple"
        />
      </div>

      {/* Margin Stats (only show if we have cost data) */}
      {(summary.total_cost > 0 || summary.total_margin > 0) && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <HeroStat
            title="Total Cost"
            value={`$${(summary.total_cost || 0).toFixed(2)}`}
            subtitle="materials + overhead"
            icon={DollarSign}
            color="red"
          />
          <HeroStat
            title="Net Margin"
            value={`$${(summary.total_margin || 0).toFixed(2)}`}
            subtitle={`${(summary.margin_percent || 0).toFixed(1)}% margin`}
            icon={TrendingUp}
            color="green"
          />
          <HeroStat
            title="Projected Cost"
            value={`$${(summary.projected_cost || 0).toFixed(2)}`}
            subtitle="pending pipeline"
            icon={Target}
            color="orange"
          />
          <HeroStat
            title="Cost Tracked"
            value={`${summary.jobs_with_cost_data || 0}/${summary.completed_jobs}`}
            subtitle="jobs with cost data"
            icon={Activity}
            color="cyan"
          />
        </div>
      )}

      {/* Charts Row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <JobsOverTimeChart data={jobs_by_date} />
        <CostRevenueChart data={jobs_by_date} />
        <FleetOverview summary={summary} />
      </div>

      {/* Rankings */}
      <ModelRankings topData={top_by_hour} worstData={worst_performers} />

      {/* Failure Analytics */}
      {failureLoading ? (
        <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 animate-pulse">
          <div className="h-6 bg-farm-800 rounded w-48 mb-4"></div>
          <div className="h-48 bg-farm-800/50 rounded"></div>
        </div>
      ) : failureData && failureData.total_failed > 0 && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <FailureRateByPrinterChart data={failureData} />
          <TopFailureReasons data={failureData} />
        </div>
      )}

      {/* Bottom Row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <PrinterUtilization data={printer_stats} />
        {timeAccuracyLoading ? (
          <div className="bg-farm-900 rounded-lg border border-farm-800 p-4 animate-pulse">
            <div className="h-6 bg-farm-800 rounded w-48 mb-4"></div>
            <div className="h-48 bg-farm-800/50 rounded"></div>
          </div>
        ) : (
          <TimeAccuracyChart data={timeAccuracy} />
        )}
        <EnergyWidget jobs={energyJobs || []} />
      </div>
    </div>
  )
}
