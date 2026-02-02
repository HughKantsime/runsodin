import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { 
  Play, 
  Clock, 
  CheckCircle, 
  AlertCircle,
  Zap,
  XCircle,
  Activity,
  Timer,
  Video
} from 'lucide-react'
import clsx from 'clsx'
import CameraModal from '../components/CameraModal'

import { stats, jobs, scheduler, printers, printJobs } from '../api'
import { canDo } from '../permissions'

function StatCard({ label, value, icon: Icon, color = 'farm', trend }) {
  const colorClasses = {
    farm: 'bg-farm-900 text-farm-100',
    print: 'bg-print-900/30 text-print-400',
    pending: 'bg-amber-900/30 text-amber-400',
    scheduled: 'bg-blue-900/30 text-blue-400',
  }

  return (
    <div className={clsx('rounded-xl p-4 md:p-5', colorClasses[color])}>
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs md:text-sm text-farm-400 mb-1">{label}</p>
          <p className="text-2xl md:text-3xl font-bold font-display">{value}</p>
          {trend && <p className="text-xs text-farm-500 mt-2">{trend}</p>}
        </div>
        <div className="p-2 bg-farm-800/50 rounded-lg">
          <Icon size={20} className="md:w-6 md:h-6" />
        </div>
      </div>
    </div>
  )
}

function PrinterCard({ printer, hasCamera, onCameraClick, printerStats }) {
  const statusColor = printer.is_active ? 'text-print-400' : 'text-farm-500'
  const pStats = printerStats?.find(s => s.printer_id === printer.id)
  
  const getShortName = (color) => {
    if (!color) return ''
    const parts = color.split(' ')
    if (parts.length > 2) return parts.slice(2).join(' ').toLowerCase()
    return color.toLowerCase()
  }

  const slots = printer.filament_slots || []
  
  return (
    <div className="bg-farm-900 rounded-xl p-4 border border-farm-800">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-display font-semibold text-base md:text-lg truncate mr-2">{printer.name}</h3>
        <div className="flex items-center gap-2 md:gap-3 flex-shrink-0">
          {pStats && (
            <div className="flex items-center gap-1 text-xs hidden sm:flex">
              <Timer size={12} className="text-farm-400" />
              <span className="text-print-400 font-medium">{pStats.total_hours?.toFixed(1) || 0}h</span>
              <span className="text-farm-500">({pStats.completed_jobs || 0})</span>
            </div>
          )}
          <div className="flex items-center gap-1.5">
            <div className={`w-2 h-2 rounded-full ${printer.is_active ? "bg-green-500" : "bg-red-500"}`}></div>
            <span className={clsx("text-xs", statusColor)}>{printer.is_active ? "Active" : "Inactive"}</span>
          {hasCamera && (
            <button onClick={(e) => { e.stopPropagation(); onCameraClick(printer) }} className="p-1 hover:bg-farm-700 rounded transition-colors" title="View camera">
              <Video size={14} className="text-farm-400" />
            </button>
          )}
          </div>
        </div>
      </div>
      <div className="grid grid-cols-4 gap-1.5 md:gap-2">
        {slots.map((slot, idx) => (
          <div key={idx} className="bg-farm-800 rounded-lg p-1.5 md:p-2 text-center min-w-0">
            <div 
              className="w-full h-2.5 md:h-3 rounded mb-1" 
              style={{ backgroundColor: slot.color_hex ? `#${slot.color_hex}` : (slot.color ? '#888' : '#333') }} 
            />
            <span className="text-[10px] md:text-xs text-farm-500 truncate block">{getShortName(slot.color)}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
function formatHours(hours) {
  if (!hours) return "—"
  const mins = Math.round(hours * 60)
  if (mins < 60) return `${mins}m`
  const hrs = Math.floor(mins / 60)
  const m = mins % 60
  return m > 0 ? `${hrs}h ${m}m` : `${hrs}h`
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
    <div className={clsx('bg-farm-900 rounded-lg p-3 md:p-4 border-l-4', statusColors[job.status])}>
      <div className="flex items-center justify-between">
        <div className="min-w-0 mr-2">
          <h4 className="font-medium truncate">{job.item_name}</h4>
          <p className="text-sm text-farm-500 truncate">{job.printer?.name || 'Unassigned'} • {formatHours(job.duration_hours)}</p>
        </div>
        <div className="flex items-center gap-1.5 md:gap-2 flex-shrink-0">
          {canDo('dashboard.actions') && job.status === 'scheduled' && (
            <button onClick={() => onStart(job.id)} className="p-1.5 md:p-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors" title="Start Print">
              <Play size={14} className="md:w-4 md:h-4" />
            </button>
          )}
          {canDo('dashboard.actions') && job.status === 'printing' && (
            <button onClick={() => onComplete(job.id)} className="p-1.5 md:p-2 bg-green-600 hover:bg-green-500 rounded-lg transition-colors" title="Mark Complete">
              <CheckCircle size={14} className="md:w-4 md:h-4" />
            </button>
          )}
          {canDo('dashboard.actions') && (job.status === 'scheduled' || job.status === 'printing' || job.status === 'pending') && (
            <button onClick={() => onCancel(job.id)} className="p-1.5 md:p-2 bg-farm-700 hover:bg-red-600 rounded-lg transition-colors" title="Cancel">
              <XCircle size={14} className="md:w-4 md:h-4" />
            </button>
          )}
          <div className={clsx('status-dot', job.status)} />
        </div>
      </div>
      {job.colors_list?.length > 0 && (
        <div className="flex gap-1 mt-2">
          {job.colors_list.map((color, i) => (
            <div key={i} className="w-4 h-4 md:w-5 md:h-5 rounded-full border border-farm-600" style={{ backgroundColor: color }} title={color} />
          ))}
        </div>
      )}
    </div>
  )
}

function MqttPrintItem({ job }) {
  const formatTime = (isoString) => {
    if (!isoString) return '—'
    const d = new Date(isoString)
    return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
  }

  return (
    <div className="bg-farm-900 rounded-lg p-3 md:p-4 border-l-4 border-print-500">
      <div className="flex items-center justify-between">
        <div className="min-w-0 mr-2">
          <h4 className="font-medium flex items-center gap-2 truncate">
            <Activity size={14} className="text-print-400 animate-pulse flex-shrink-0" />
            <span className="truncate">{job.job_name || 'Unknown'}</span>
          </h4>
          <p className="text-sm text-farm-500 truncate">{job.printer_name} • Started {formatTime(job.started_at)}</p>
        </div>
        <div className="text-right flex-shrink-0">
          {job.total_layers && <p className="text-xs text-farm-500">{job.total_layers} layers</p>}
        </div>
      </div>
    </div>
  )
}

function PrintHistoryItem({ job }) {
  const statusColors = {
    running: "border-yellow-500",
    completed: "border-gray-500",
    failed: "border-red-500",
    cancelled: "border-gray-500",
  }

  const formatDuration = (minutes) => {
    if (!minutes) return '—'
    if (minutes < 60) return `${Math.round(minutes)}m`
    const hrs = Math.floor(minutes / 60)
    const mins = Math.round(minutes % 60)
    return `${hrs}h ${mins}m`
  }

  const formatTime = (isoString) => {
    if (!isoString) return '—'
    const d = new Date(isoString)
    return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })
  }

  return (
    <div className={clsx('bg-farm-900 rounded-lg p-3 md:p-4 border-l-4', statusColors[job.status] || 'border-farm-700')}>
      <div className="flex items-center justify-between">
        <div className="min-w-0 mr-2">
          <h4 className="font-medium truncate">{job.job_name || 'Unknown'}</h4>
          <p className="text-sm text-farm-500 truncate">{job.printer_name} • {formatTime(job.started_at)}</p>
        </div>
        <div className="text-right flex-shrink-0">
          <div className="font-medium text-print-400">{formatDuration(job.duration_minutes)}</div>
          {job.total_layers && <p className="text-xs text-farm-500">{job.total_layers} layers</p>}
        </div>
      </div>
    </div>
  )
}

export default function Dashboard() {
  const queryClient = useQueryClient()

  const [cameraTarget, setCameraTarget] = useState(null)
  const { data: activeCameras } = useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const token = localStorage.getItem('token')
      const headers = { 'X-API-Key': '5464389e808f206efd9f9febef7743ff7a16911797cb0f058e805c82b33396ce' }
      if (token) headers['Authorization'] = 'Bearer ' + token
      const response = await fetch('/api/cameras', { headers })
      if (!response.ok) return []
      return response.json()
    }
  })
  const cameraIds = new Set((activeCameras || []).map(c => c.id))

  const { data: statsData } = useQuery({ queryKey: ['stats'], queryFn: stats.get })
  const { data: printersData } = useQuery({ queryKey: ['printers'], queryFn: () => printers.list(true) })
  const { data: activeJobs } = useQuery({ queryKey: ['jobs', 'active'], queryFn: () => jobs.list() })
  const { data: allPrintJobs } = useQuery({ queryKey: ['print-jobs'], queryFn: () => printJobs.list({ limit: 20 }), refetchInterval: 5000 })
  const { data: printerStats } = useQuery({ queryKey: ['print-jobs-stats'], queryFn: printJobs.stats, refetchInterval: 30000 })

  // Split MQTT jobs into running vs completed
  const runningMqttJobs = allPrintJobs?.filter(j => j.status === 'running') || []
  const completedMqttJobs = allPrintJobs?.filter(j => j.status !== 'running') || []

  const runScheduler = useMutation({
    mutationFn: scheduler.run,
    onSuccess: () => { queryClient.invalidateQueries(['jobs']); queryClient.invalidateQueries(['stats']) },
  })

  const startJob = useMutation({
    mutationFn: jobs.start,
    onSuccess: () => { queryClient.invalidateQueries(['jobs']); queryClient.invalidateQueries(['stats']) },
  })

  const completeJob = useMutation({
    mutationFn: jobs.complete,
    onSuccess: () => { queryClient.invalidateQueries(['jobs']); queryClient.invalidateQueries(['stats']) },
  })

  const cancelJob = useMutation({
    mutationFn: jobs.cancel,
    onSuccess: () => { queryClient.invalidateQueries(['jobs']); queryClient.invalidateQueries(['stats']) },
  })

  // Calculate currently printing count (scheduled jobs + MQTT running)
  const currentlyPrinting = statsData?.jobs?.printing || 0

  return (
    <div className="p-4 md:p-8">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between mb-6 md:mb-8 gap-4">
        <div>
          <h1 className="text-2xl md:text-3xl font-display font-bold">Dashboard</h1>
          <p className="text-farm-500 mt-1 text-sm md:text-base">Print farm overview</p>
        </div>
        {canDo('dashboard.actions') && <button onClick={() => runScheduler.mutate()} disabled={runScheduler.isPending}
          className={clsx('flex items-center gap-2 px-4 md:px-5 py-2 md:py-2.5 rounded-lg font-medium transition-colors bg-print-600 hover:bg-print-500 text-white text-sm md:text-base self-start sm:self-auto', runScheduler.isPending && 'opacity-50 cursor-not-allowed')}>
          <Zap size={18} />
          {runScheduler.isPending ? 'Scheduling...' : 'Run Scheduler'}
        </button>}
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 md:gap-4 mb-6 md:mb-8">
        <StatCard label="Currently Printing" value={currentlyPrinting} icon={Play} color="print" />
        <StatCard label="Scheduled" value={statsData?.jobs?.scheduled || 0} icon={Clock} color="scheduled" />
        <StatCard label="Pending" value={statsData?.jobs?.pending || 0} icon={AlertCircle} color="pending" />
        <StatCard label="Completed Today" value={statsData?.jobs?.completed_today || 0} icon={CheckCircle} color="farm" />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 md:gap-8">
        {/* Printers */}
        <div className="lg:col-span-2">
          <h2 className="text-lg md:text-xl font-display font-semibold mb-4">Printers</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {printersData?.map((printer) => (
              <PrinterCard key={printer.id} printer={printer} printerStats={printerStats} hasCamera={cameraIds.has(printer.id)} onCameraClick={setCameraTarget} />
            ))}
            {(!printersData || printersData.length === 0) && (
              <div className="col-span-1 md:col-span-2 bg-farm-900 rounded-xl p-8 text-center text-farm-500">No printers configured.</div>
            )}
          </div>
        </div>

        {/* Sidebar */}
        <div className="space-y-6">
          {/* Active Jobs - includes running MQTT prints */}
          <div>
            <h2 className="text-lg md:text-xl font-display font-semibold mb-4">Active Jobs</h2>
            <div className="space-y-3">
              {/* Running MQTT prints */}
              {runningMqttJobs.map((job) => (
                <MqttPrintItem key={`mqtt-${job.id}`} job={job} />
              ))}
              {/* Scheduled jobs from Jobs table */}
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
              {runningMqttJobs.length === 0 && (!activeJobs || activeJobs.filter(j => ['printing', 'scheduled', 'pending'].includes(j.status)).length === 0) && (
                <div className="bg-farm-900 rounded-xl p-6 text-center text-farm-500 text-sm">No active jobs</div>
              )}
            </div>
          </div>

          {/* Recent Prints - only completed/failed/cancelled */}
          <div>
            <h2 className="text-lg md:text-xl font-display font-semibold mb-4">Recent Prints</h2>
            <div className="space-y-3">
              {completedMqttJobs.slice(0, 10).map((job) => <PrintHistoryItem key={job.id} job={job} />)}
              {completedMqttJobs.length === 0 && (
                <div className="bg-farm-900 rounded-xl p-6 text-center text-farm-500 text-sm">No print history yet</div>
              )}
            </div>
          </div>
        </div>
      </div>

      {runScheduler.isSuccess && (
        <div className="fixed bottom-4 right-4 bg-print-600 text-white px-4 py-3 rounded-lg shadow-lg z-30">
          Scheduled {runScheduler.data?.scheduled || 0} jobs
        </div>
      )}
      {cameraTarget && <CameraModal printer={cameraTarget} onClose={() => setCameraTarget(null)} />}
    </div>
  )
}
