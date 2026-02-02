import { useQuery } from '@tanstack/react-query'
import { analytics } from '../api'
import { 
  TrendingUp, TrendingDown, DollarSign, Clock, Printer, 
  Package, BarChart3, Target
} from 'lucide-react'

function StatCard({ title, value, subtitle, icon: Icon, color = "text-print-400" }) {
  return (
    <div className="bg-farm-900 rounded-xl border border-farm-800 p-3 md:p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs md:text-sm text-farm-400">{title}</span>
        {Icon && <Icon size={16} className="text-farm-500" />}
      </div>
      <div className={`text-xl md:text-2xl font-display font-bold ${color}`}>{value}</div>
      {subtitle && <div className="text-xs text-farm-500 mt-1">{subtitle}</div>}
    </div>
  )
}

function RankingTable({ title, data, valueKey, valueLabel, icon: Icon, ascending = false }) {
  return (
    <div className="bg-farm-900 rounded-xl border border-farm-800 p-3 md:p-4">
      <div className="flex items-center gap-2 mb-4">
        {Icon && <Icon size={16} className="text-farm-400" />}
        <h3 className="font-display font-semibold text-sm md:text-base">{title}</h3>
      </div>
      <div className="space-y-2">
        {data.map((item, i) => (
          <div key={item.id} className="flex items-center justify-between py-2 border-b border-farm-800 last:border-0">
            <div className="flex items-center gap-2 md:gap-3 min-w-0">
              <span className={`w-5 h-5 md:w-6 md:h-6 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0 ${
                ascending 
                  ? (i < 3 ? 'bg-red-900 text-red-300' : 'bg-farm-800 text-farm-400')
                  : (i < 3 ? 'bg-print-900 text-print-300' : 'bg-farm-800 text-farm-400')
              }`}>
                {i + 1}
              </span>
              <span className="text-sm truncate">{item.name}</span>
            </div>
            <div className="text-right flex-shrink-0 ml-2">
              <div className={`font-medium text-sm ${ascending ? 'text-red-400' : 'text-print-400'}`}>
                ${item[valueKey]?.toFixed(2)}/hr
              </div>
              <div className="text-xs text-farm-500">
                ${item.value_per_bed?.toFixed(2)}/bed • {item.build_time_hours}h
              </div>
            </div>
          </div>
        ))}
        {data.length === 0 && (
          <div className="text-center text-farm-500 py-4 text-sm">No data yet</div>
        )}
      </div>
    </div>
  )
}

function PrinterStatsTable({ data }) {
  return (
    <div className="bg-farm-900 rounded-xl border border-farm-800 p-3 md:p-4">
      <div className="flex items-center gap-2 mb-4">
        <Printer size={16} className="text-farm-400" />
        <h3 className="font-display font-semibold text-sm md:text-base">Printer Utilization</h3>
      </div>
      <div className="space-y-2">
        {data.map((printer) => (
          <div key={printer.id} className="flex items-center justify-between py-2 border-b border-farm-800 last:border-0">
            <span className="text-sm">{printer.name}</span>
            <div className="text-right">
              <div className="font-medium text-sm">{printer.completed_jobs} jobs</div>
              <div className="text-xs text-farm-500">{printer.total_hours} hours</div>
            </div>
          </div>
        ))}
        {data.length === 0 && (
          <div className="text-center text-farm-500 py-4 text-sm">No printer data</div>
        )}
      </div>
    </div>
  )
}

export default function Analytics() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['analytics'],
    queryFn: analytics.get,
  })

  if (isLoading) {
    return (
      <div className="p-4 md:p-6">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-farm-800 rounded w-48"></div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4">
            {[...Array(4)].map((_, i) => (
              <div key={i} className="h-24 bg-farm-800 rounded-xl"></div>
            ))}
          </div>
        </div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-4 md:p-6">
        <div className="bg-red-900/50 border border-red-700 rounded-xl p-4 text-red-300 text-sm">
          Error loading analytics: {error.message}
        </div>
      </div>
    )
  }

  const { summary, top_by_hour, worst_performers, printer_stats } = data

  return (
    <div className="p-4 md:p-6 space-y-4 md:space-y-6">
      <div>
        <h1 className="text-xl md:text-2xl font-display font-bold">Analytics</h1>
        <p className="text-farm-400 text-sm">Profitability and usage insights</p>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4">
        <StatCard
          title="Total Revenue"
          value={`$${summary.total_revenue.toFixed(2)}`}
          subtitle={`from ${summary.completed_jobs} completed jobs`}
          icon={DollarSign}
          color="text-green-400"
        />
        <StatCard
          title="Projected Revenue"
          value={`$${summary.projected_revenue.toFixed(2)}`}
          subtitle={`from ${summary.pending_jobs} pending jobs`}
          icon={Target}
          color="text-yellow-400"
        />
        <StatCard
          title="Avg $/Hour"
          value={`$${summary.avg_value_per_hour.toFixed(2)}`}
          subtitle="across all models"
          icon={TrendingUp}
          color="text-print-400"
        />
        <StatCard
          title="Print Hours"
          value={summary.total_print_hours}
          subtitle={`${summary.total_models} models in library`}
          icon={Clock}
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 md:gap-6">
        <RankingTable
          title="Top Performers ($/Hour)"
          data={top_by_hour}
          valueKey="value_per_hour"
          valueLabel="$/hr"
          icon={TrendingUp}
        />
        <RankingTable
          title="Worst Performers ($/Hour)"
          data={worst_performers}
          valueKey="value_per_hour"
          valueLabel="$/hr"
          icon={TrendingDown}
          ascending
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 md:gap-6">
        <PrinterStatsTable data={printer_stats} />
        
        <div className="bg-farm-900 rounded-xl border border-farm-800 p-3 md:p-4">
          <div className="flex items-center gap-2 mb-4">
            <BarChart3 size={16} className="text-farm-400" />
            <h3 className="font-display font-semibold text-sm md:text-base">Quick Stats</h3>
          </div>
          <div className="grid grid-cols-2 gap-3 md:gap-4">
            <div className="text-center p-3 bg-farm-800 rounded-lg">
              <div className="text-xl md:text-2xl font-bold">{summary.total_models}</div>
              <div className="text-xs text-farm-400">Total Models</div>
            </div>
            <div className="text-center p-3 bg-farm-800 rounded-lg">
              <div className="text-xl md:text-2xl font-bold">{summary.total_jobs}</div>
              <div className="text-xs text-farm-400">Total Jobs</div>
            </div>
            <div className="text-center p-3 bg-farm-800 rounded-lg">
              <div className="text-xl md:text-2xl font-bold text-green-400">{summary.completed_jobs}</div>
              <div className="text-xs text-farm-400">Completed</div>
            </div>
            <div className="text-center p-3 bg-farm-800 rounded-lg">
              <div className="text-xl md:text-2xl font-bold text-yellow-400">{summary.pending_jobs}</div>
              <div className="text-xs text-farm-400">Pending</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
