import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { 
  Play, 
  Clock, 
  CheckCircle, 
  AlertCircle,
  Zap,
  RefreshCw,
  XCircle,
  Trash2
} from 'lucide-react'
import clsx from 'clsx'

import { stats, jobs, scheduler, printers } from '../api'

function StatCard({ label, value, icon: Icon, color = 'farm', trend }) {
  const colorClasses = {
    farm: 'bg-farm-900 text-farm-100',
    print: 'bg-print-900/30 text-print-400',
    pending: 'bg-amber-900/30 text-amber-400',
    scheduled: 'bg-blue-900/30 text-blue-400',
  }

  return (
    <div className={clsx('rounded-xl p-5', colorClasses[color])}>
      <div className="flex items-start justify-between">
        <div>
          <p className="text-sm text-farm-400 mb-1">{label}</p>
          <p className="text-3xl font-bold font-display">{value}</p>
          {trend && (
            <p className="text-xs text-farm-500 mt-2">{trend}</p>
          )}
        </div>
        <div className="p-2 bg-farm-800/50 rounded-lg">
          <Icon size={24} />
        </div>
      </div>
    </div>
  )
}

function PrinterCard({ printer }) {
  const statusColor = printer.is_active ? 'text-print-400' : 'text-farm-500'
  
  // Extract just the color name (last part after brand)
  const getShortName = (color) => {
    if (!color) return '—'
    // If it has a brand prefix like "Bambu Lab Black", just show "black"
    const parts = color.split(' ')
    if (parts.length > 2) {
      return parts.slice(2).join(' ').toLowerCase()
    }
    return color.toLowerCase()
  }
  
  return (
    <div className="bg-farm-900 rounded-xl p-4 border border-farm-800 h-fit">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-display font-semibold text-lg">{printer.name}</h3>
        <div className="flex items-center gap-1.5">
          <div className={`w-2 h-2 rounded-full ${printer.is_active ? "bg-green-500" : "bg-red-500"}`}></div>
          <span className={clsx("text-xs", statusColor)}>{printer.is_active ? "Active" : "Inactive"}</span>
        </div>
      </div>
      {/* Filament slots - grid layout */}
      <div className="grid grid-cols-4 gap-2">
        {printer.filament_slots?.map((slot) => (
          <div 
            key={slot.slot_number}
            className="bg-farm-800 rounded-lg p-2 text-center min-w-0"
            title={slot.color || 'Empty'}
          >
            <div 
              className="w-full h-3 rounded mb-1"
              style={{ 
                backgroundColor: slot.color_hex ? `#${slot.color_hex}` : (slot.color ? '#888' : '#333')
              }}
            />
            <span className="text-xs text-farm-500 truncate block">
              {getShortName(slot.color)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

function JobQueueItem({ job, onStart, onComplete, onCancel }) {
  const statusColors = {
    pending: 'border-status-pending',
    scheduled: 'border-status-scheduled',
    printing: 'border-status-printing',
    completed: 'border-status-completed',
    failed: 'border-status-failed',
  }

  return (
    <div className={clsx(
      'bg-farm-900 rounded-lg p-4 border-l-4',
      statusColors[job.status]
    )}>
      <div className="flex items-center justify-between">
        <div>
          <h4 className="font-medium">{job.item_name}</h4>
          <p className="text-sm text-farm-500">
            {job.printer_name || 'Unassigned'} • {job.duration_hours || job.effective_duration}h
          </p>
        </div>
        
        <div className="flex items-center gap-2">
          {job.status === 'scheduled' && (
            <button
              onClick={() => onStart(job.id)}
              className="p-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors"
              title="Start Print"
            >
              <Play size={16} />
            </button>
          )}
          {job.status === 'printing' && (
            <button
              onClick={() => onComplete(job.id)}
              className="p-2 bg-green-600 hover:bg-green-500 rounded-lg transition-colors"
              title="Mark Complete"
            >
              <CheckCircle size={16} />
            </button>
          )}
          {(job.status === 'scheduled' || job.status === 'printing' || job.status === 'pending') && (
            <button
              onClick={() => onCancel(job.id)}
              className="p-2 bg-farm-700 hover:bg-red-600 rounded-lg transition-colors"
              title="Cancel"
            >
              <XCircle size={16} />
            </button>
          )}
          <div className={clsx('status-dot', job.status)} />
        </div>
      </div>
      
      {/* Color chips */}
      {job.colors_list?.length > 0 && (
        <div className="flex gap-1 mt-2">
          {job.colors_list.map((color, i) => (
            <span 
              key={i}
              className="text-xs bg-farm-800 px-2 py-0.5 rounded"
            >
              {color}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

export default function Dashboard() {
  const queryClient = useQueryClient()

  const { data: statsData, isLoading: statsLoading } = useQuery({
    queryKey: ['stats'],
    queryFn: stats.get,
  })

  const { data: printersData } = useQuery({
    queryKey: ['printers'],
    queryFn: () => printers.list(true),
  })

  const { data: activeJobs } = useQuery({
    queryKey: ['jobs', 'active'],
    queryFn: () => jobs.list(),
  })

  const runScheduler = useMutation({
    mutationFn: scheduler.run,
    onSuccess: () => {
      queryClient.invalidateQueries(['jobs'])
      queryClient.invalidateQueries(['stats'])
    },
  })

  const startJob = useMutation({
    mutationFn: jobs.start,
    onSuccess: () => {
      queryClient.invalidateQueries(['jobs'])
      queryClient.invalidateQueries(['stats'])
    },
  })

  const completeJob = useMutation({
    mutationFn: jobs.complete,
    onSuccess: () => {
      queryClient.invalidateQueries(['jobs'])
      queryClient.invalidateQueries(['stats'])
    },
  })

  return (
    <div className="p-8">
      {/* Header */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-3xl font-display font-bold">Dashboard</h1>
          <p className="text-farm-500 mt-1">Print farm overview</p>
        </div>
        
        <button
          onClick={() => runScheduler.mutate()}
          disabled={runScheduler.isPending}
          className={clsx(
            'flex items-center gap-2 px-5 py-2.5 rounded-lg font-medium transition-colors',
            'bg-print-600 hover:bg-print-500 text-white',
            runScheduler.isPending && 'opacity-50 cursor-not-allowed'
          )}
        >
          <Zap size={18} />
          {runScheduler.isPending ? 'Scheduling...' : 'Run Scheduler'}
        </button>
      </div>

      {/* Stats Grid */}
      <div className="grid grid-cols-4 gap-4 mb-8">
        <StatCard
          label="Currently Printing"
          value={statsData?.jobs?.printing || 0}
          icon={Play}
          color="print"
        />
        <StatCard
          label="Scheduled"
          value={statsData?.jobs?.scheduled || 0}
          icon={Clock}
          color="scheduled"
        />
        <StatCard
          label="Pending"
          value={statsData?.jobs?.pending || 0}
          icon={AlertCircle}
          color="pending"
        />
        <StatCard
          label="Completed Today"
          value={statsData?.jobs?.completed_today || 0}
          icon={CheckCircle}
          color="farm"
        />
      </div>

      <div className="grid grid-cols-3 gap-8">
        {/* Printers */}
        <div className="col-span-2">
          <h2 className="text-xl font-display font-semibold mb-4">Printers</h2>
          <div className="grid grid-cols-2 gap-4 items-start">
            {printersData?.map((printer) => (
              <PrinterCard key={printer.id} printer={printer} />
            ))}
            {(!printersData || printersData.length === 0) && (
              <div className="col-span-2 bg-farm-900 rounded-xl p-8 text-center text-farm-500">
                No printers configured. Add printers to get started.
              </div>
            )}
          </div>
        </div>

        {/* Job Queue */}
        <div>
          <h2 className="text-xl font-display font-semibold mb-4">Active Jobs</h2>
          <div className="space-y-3">
            {activeJobs?.filter(j => ['printing', 'scheduled', 'pending'].includes(j.status))
              .slice(0, 8)
              .map((job) => (
                <JobQueueItem 
                  key={job.id} 
                  job={job}
                  onStart={(id) => startJob.mutate(id)}
                  onComplete={(id) => completeJob.mutate(id)}
                  onCancel={(id) => cancelJob.mutate(id)}
                />
              ))}
            {(!activeJobs || activeJobs.length === 0) && (
              <div className="bg-farm-900 rounded-xl p-8 text-center text-farm-500">
                No active jobs. Create jobs to schedule prints.
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Scheduler Result Toast */}
      {runScheduler.isSuccess && (
        <div className="fixed bottom-4 right-4 bg-print-600 text-white px-4 py-3 rounded-lg shadow-lg">
          Scheduled {runScheduler.data?.scheduled || 0} jobs
        </div>
      )}
    </div>
  )
}
