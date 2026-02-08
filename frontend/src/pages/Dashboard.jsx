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
  Video,
  AlertTriangle,
  Lightbulb,
} from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import clsx from 'clsx'
import CameraModal from '../components/CameraModal'

import { stats, jobs, scheduler, printers, printJobs, alerts as alertsApi } from '../api'
import { canDo } from '../permissions'

function StatCard({ label, value, icon: Icon, color = 'farm', trend }) {
  const colorClasses = {
    farm: 'bg-farm-900 text-farm-100',
    print: 'bg-print-900/30 text-print-400',
    pending: 'bg-amber-900/30 text-amber-400',
    scheduled: 'bg-blue-900/30 text-blue-400',
    alert: 'bg-red-900/30 text-red-400',
    maintenance: 'bg-purple-900/30 text-purple-400',
  }

  return (
    <div className={clsx('rounded p-3 md:p-4', colorClasses[color])}>
      <div className="flex items-start justify-between">
        <div>
          <p className="text-xs md:text-sm text-farm-400 mb-1">{label}</p>
          <p className="text-xl md:text-2xl font-bold font-display">{value}</p>
          {trend && <p className="text-xs text-farm-500 mt-2">{trend}</p>}
        </div>
        <div className="p-1.5 bg-farm-800/50 rounded-lg">
          <Icon size={20} className="md:w-6 md:h-6" />
        </div>
      </div>
    </div>
  )
}

function PrinterCard({ printer, hasCamera, onCameraClick, activeJob }) {
  const getShortName = (color) => {
    if (!color) return ''
    const parts = color.split(' ')
    if (parts.length > 2) return parts.slice(2).join(' ').toLowerCase()
    return color.toLowerCase()
  }

  const slots = printer.filament_slots || []
  const LOW_SPOOL_THRESHOLD = 100
  const lowSpools = slots.filter(s => s.remaining_weight && s.remaining_weight < LOW_SPOOL_THRESHOLD)
  const hasLowSpool = lowSpools.length > 0
  
  // Live telemetry
  const online = printer.last_seen && (Date.now() - new Date(printer.last_seen + 'Z').getTime()) < 90000
  const bedTemp = printer.bed_temp != null ? Math.round(printer.bed_temp) : null
  const nozTemp = printer.nozzle_temp != null ? Math.round(printer.nozzle_temp) : null
  const bedTarget = printer.bed_target_temp != null ? Math.round(printer.bed_target_temp) : null
  const nozTarget = printer.nozzle_target_temp != null ? Math.round(printer.nozzle_target_temp) : null
  const isHeating = (bedTarget && bedTarget > 0) || (nozTarget && nozTarget > 0)
  const stage = printer.print_stage && printer.print_stage !== 'Idle' ? printer.print_stage : null
  
  return (
    <div className={clsx("bg-farm-900 rounded border overflow-hidden h-fit", hasLowSpool ? "border-amber-600/50" : "border-farm-800")}>
      <div className="p-3 md:p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-display font-semibold text-base md:text-lg truncate mr-2">{printer.nickname || printer.name}</h3>
          <div className="flex items-center gap-2 flex-shrink-0">
            {hasCamera && (
              <button onClick={(e) => { e.stopPropagation(); onCameraClick(printer) }} className="p-1 hover:bg-farm-700 rounded transition-colors" title="View camera">
                <Video size={14} className="text-farm-400" />
              </button>
            )}
          </div>
        </div>
        {activeJob && (
          <div className="mb-3 bg-farm-800 rounded-lg p-3">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2 min-w-0">
                <Activity size={14} className="text-print-400 animate-pulse flex-shrink-0" />
                <span className="text-sm font-medium truncate">{activeJob.job_name || 'Printing'}</span>
              </div>
              <div className="text-right flex-shrink-0">
                <span className="text-lg font-bold text-green-400">{activeJob.progress_percent || 0}%</span>
              </div>
            </div>
            <div className="w-full bg-farm-700 rounded-full h-2 mb-1.5">
              <div 
                className="bg-green-500 h-2 rounded-full transition-all duration-500"
                style={{ width: `${activeJob.progress_percent || 0}%` }}
              />
            </div>
            <div className="flex justify-between text-xs text-farm-500">
              <span>
                {activeJob.current_layer && activeJob.total_layers 
                  ? `Layer ${activeJob.current_layer}/${activeJob.total_layers}`
                  : activeJob.total_layers 
                    ? `${activeJob.total_layers} layers`
                    : ''}
              </span>
              <span>
                {activeJob.remaining_minutes 
                  ? activeJob.remaining_minutes < 60 
                    ? `${Math.round(activeJob.remaining_minutes)}m left`
                    : `${Math.floor(activeJob.remaining_minutes / 60)}h ${Math.round(activeJob.remaining_minutes % 60)}m left`
                  : ''}
              </span>
            </div>
          </div>
        )}

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
      {/* Bottom status bar */}
      <div className="px-3 md:px-4 py-2 md:py-3 bg-farm-950 border-t border-farm-800">
        <div className="flex items-center justify-between text-xs">
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1.5">
            <div className={`w-2 h-2 rounded-full ${online ? "bg-green-500" : "bg-farm-600"}`}></div>
            <span className={online ? "text-green-400" : "text-farm-500"}>{online ? "Online" : "Offline"}</span>
          </div>
          {printer.lights_on != null && (
            <button
              onClick={(e) => { e.stopPropagation(); printers.toggleLights(printer.id) }}
              className={`p-0.5 rounded transition-colors ${printer.lights_on ? 'text-yellow-400 hover:text-yellow-300' : 'text-farm-600 hover:text-farm-400'}`}
              title={printer.lights_on ? 'Lights on (click to toggle)' : 'Lights off (click to toggle)'}
            >
              <Lightbulb size={14} />
            </button>
          )}
        </div>
        <div className="flex items-center gap-3 text-farm-400">
          {nozTemp != null && (
            <span className={isHeating ? "text-orange-400" : ""} title={nozTarget > 0 ? `Nozzle: ${nozTemp}°/${nozTarget}°C` : `Nozzle: ${nozTemp}°C`}>
              Nozzle {nozTemp}°{nozTarget > 0 ? `/${nozTarget}°` : ''}
            </span>
          )}
          {bedTemp != null && (
            <span className={bedTarget > 0 ? "text-orange-400" : ""} title={bedTarget > 0 ? `Bed: ${bedTemp}°/${bedTarget}°C` : `Bed: ${bedTemp}°C`}>
              Bed {bedTemp}°{bedTarget > 0 ? `/${bedTarget}°` : ''}
            </span>
          )}
          {stage && (
            <span className="text-print-400">{stage}</span>
          )}
        </div>
        </div>
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
            <span className={clsx('px-2 py-1 rounded text-xs font-medium',
              job.status === 'scheduled' ? 'bg-blue-900/30 text-blue-400' :
              job.status === 'pending' ? 'bg-amber-900/30 text-amber-400' :
              'bg-farm-800 text-farm-400'
            )}>{job.status}</span>
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
          <p className="text-lg font-bold text-print-400">{job.progress_percent || 0}%</p>
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


function AlertsWidget() {
  const navigate = useNavigate()
  const { data: summary } = useQuery({
    queryKey: ['alert-summary'],
    queryFn: alertsApi.summary,
    refetchInterval: 60000,
  })

  if (!summary || summary.total === 0) return null

  const items = [
    { key: 'print_failed', count: summary.print_failed, icon: '\u{1F534}', label: 'failed print', plural: 'failed prints', filter: 'critical' },
    { key: 'spool_low', count: summary.spool_low, icon: '\u{1F7E1}', label: 'low spool', plural: 'low spools', filter: 'warning' },
    { key: 'maintenance_overdue', count: summary.maintenance_overdue, icon: '\u{1F7E1}', label: 'maintenance overdue', plural: 'maintenance overdue', filter: 'warning' },
  ].filter(i => i.count > 0)

  return (
    <div className="mb-6 md:mb-8 rounded border border-amber-600/30 bg-amber-950/20 p-4">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <AlertTriangle size={16} className="text-amber-400" />
          <span className="text-sm font-semibold text-amber-300">Active Alerts</span>
        </div>
        <button
          onClick={() => navigate('/alerts')}
          className="text-xs text-amber-400 hover:text-amber-300 transition-colors"
        >
          View all \u2192
        </button>
      </div>
      <div className="space-y-1.5">
        {items.map(item => (
          <button
            key={item.key}
            onClick={() => navigate(`/alerts?filter=${item.filter}`)}
            className="flex items-center gap-2 w-full text-left hover:bg-amber-900/20 rounded-lg px-2 py-1.5 transition-colors"
          >
            <span className="text-sm">{item.icon}</span>
            <span className="text-sm text-amber-200">
              {item.count} {item.count === 1 ? item.label : item.plural}
            </span>
          </button>
        ))}
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
      const headers = { 'X-API-Key': import.meta.env.VITE_API_KEY }
      if (token) headers['Authorization'] = 'Bearer ' + token
      const response = await fetch('/api/cameras', { headers })
      if (!response.ok) return []
      return response.json()
    }
  })
  const cameraIds = new Set((activeCameras || []).map(c => c.id))

  const { data: statsData } = useQuery({ queryKey: ['stats'], queryFn: stats.get })
  const { data: dashAlertSummary } = useQuery({ queryKey: ['dash-alert-summary'], queryFn: alertsApi.summary, refetchInterval: 60000 })
  const { data: printersData } = useQuery({ queryKey: ['printers'], queryFn: () => printers.list(true), refetchInterval: 30000 })
  const { data: activeJobs } = useQuery({ queryKey: ['jobs', 'active'], queryFn: () => jobs.list() })
  const { data: allPrintJobs } = useQuery({ queryKey: ['print-jobs'], queryFn: () => printJobs.list({ limit: 20 }), refetchInterval: 30000 })

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

      </div>

      <AlertsWidget />

      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2 md:gap-3 mb-6 md:mb-8">
        <StatCard label="Currently Printing" value={currentlyPrinting} icon={Play} color="print" />
        <StatCard label="Scheduled" value={statsData?.jobs?.scheduled || 0} icon={Clock} color="scheduled" />
        <StatCard label="Pending" value={statsData?.jobs?.pending || 0} icon={AlertCircle} color="pending" />
        <StatCard label="Completed Today" value={statsData?.jobs?.completed_today || 0} icon={CheckCircle} color="farm" />
        <StatCard label="Active Alerts" value={dashAlertSummary?.total || 0} icon={AlertTriangle} color="alert" />
        <StatCard label="Maintenance Due" value={dashAlertSummary?.maintenance_overdue || 0} icon={Activity} color="maintenance" />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 md:gap-8">
        {/* Printers */}
        <div className="lg:col-span-2">
          <h2 className="text-lg md:text-xl font-display font-semibold mb-4">Printers</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {printersData?.map((printer) => (
              <PrinterCard 
                key={printer.id} 
                printer={printer} 
                hasCamera={cameraIds.has(printer.id)} 
                onCameraClick={setCameraTarget}
                activeJob={runningMqttJobs.find(j => j.printer_id === printer.id)}
              />
            ))}
            {(!printersData || printersData.length === 0) && (
              <div className="col-span-1 md:col-span-2 bg-farm-900 rounded p-8 text-center text-farm-500">No printers configured.</div>
            )}
          </div>
        </div>

        {/* Sidebar */}
        <div className="space-y-6">
          {/* Active Jobs - includes running MQTT prints */}
          <div>
            <h2 className="text-lg md:text-xl font-display font-semibold mb-4">Scheduled Jobs</h2>
            <div className="space-y-3">
              {/* Scheduled/pending jobs from Jobs table (printing jobs show on printer cards) */}
              {activeJobs?.filter(j => ['scheduled', 'pending'].includes(j.status))
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
              {!activeJobs || activeJobs.filter(j => ['scheduled', 'pending'].includes(j.status)).length === 0 && (
                <div className="bg-farm-900 rounded p-6 text-center text-farm-500 text-sm">No scheduled jobs</div>
              )}
            </div>
          </div>

          {/* Recent Prints - only completed/failed/cancelled */}
          <div>
            <h2 className="text-lg md:text-xl font-display font-semibold mb-4">Recent Prints</h2>
            <div className="space-y-3">
              {completedMqttJobs.slice(0, 10).map((job) => <PrintHistoryItem key={job.id} job={job} />)}
              {completedMqttJobs.length === 0 && (
                <div className="bg-farm-900 rounded p-6 text-center text-farm-500 text-sm">No print history yet</div>
              )}
            </div>
          </div>
        </div>
      </div>


      {cameraTarget && <CameraModal printer={cameraTarget} onClose={() => setCameraTarget(null)} />}
    </div>
  )
}
