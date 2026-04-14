import { useState, useRef, useEffect, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Play,
  Clock,
  CheckCircle,
  AlertCircle,
  XCircle,
  Activity,
  Video,
  AlertTriangle,
  Lightbulb,
  Wrench,
  Monitor,
  Loader2,
  LayoutDashboard,
} from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import clsx from 'clsx'
import toast from 'react-hot-toast'
import CameraModal from '../../components/printers/CameraModal'
import SpoolRing from '../../components/ui/SpoolRing'
import { StatCard, Button, PageHeader, EmptyState, ProgressBar } from '../../components/ui'
import SectionErrorBoundary from './SectionErrorBoundary'

import { stats, jobs, printers, printJobs, alerts as alertsApi, maintenance } from '../../api'
import { canDo } from '../../permissions'
import { getShortName, formatHours, formatTime, formatDuration, isOnline } from '../../utils/shared'

function PrinterCard({ printer, hasCamera, onCameraClick, activeJob, onClick }: { printer: any; hasCamera: boolean; onCameraClick: (id: number) => void; activeJob?: any; onClick: () => void }) {
  const slots = printer.filament_slots || []
  const LOW_SPOOL_THRESHOLD = 100
  const lowSpools = slots.filter(s => s.remaining_weight && s.remaining_weight < LOW_SPOOL_THRESHOLD)
  const hasLowSpool = lowSpools.length > 0

  // Live telemetry
  const online = isOnline(printer)
  const bedTemp = printer.bed_temp != null ? Math.round(printer.bed_temp) : null
  const nozTemp = printer.nozzle_temp != null ? Math.round(printer.nozzle_temp) : null
  const bedTarget = printer.bed_target_temp != null ? Math.round(printer.bed_target_temp) : null
  const nozTarget = printer.nozzle_target_temp != null ? Math.round(printer.nozzle_target_temp) : null
  const isHeating = (bedTarget && bedTarget > 0) || (nozTarget && nozTarget > 0)
  const stage = printer.print_stage && printer.print_stage !== 'Idle' ? printer.print_stage : null
  
  const isPrinting = printer.gcode_state === 'RUNNING' || printer.gcode_state === 'PAUSE'
  const hasError = printer.hms_errors && printer.hms_errors.length > 0
  const statusColor = hasError ? 'bg-red-500' : isPrinting ? 'bg-[var(--status-completed)]' : online ? 'bg-yellow-500' : 'bg-[var(--brand-text-muted)]'
  const statusLabel = hasError ? 'Error' : isPrinting ? 'Printing' : online ? 'Idle' : 'Offline'

  return (
    <div
      className={clsx("bg-[var(--brand-card-bg)] rounded-md border overflow-hidden h-fit cursor-pointer hover:border-[var(--brand-card-border)] transition-colors", hasLowSpool ? "border-amber-600/50" : "border-[var(--brand-card-border)]")}
      onClick={onClick}
    >
      <div className="p-3 md:p-4">
        <div className="flex items-center justify-between mb-3">
          <h3 className="font-display font-semibold text-base md:text-lg truncate mr-2">{printer.nickname || printer.name}</h3>
          <div className="flex items-center gap-2 flex-shrink-0">
            <span className="flex items-center gap-1.5 text-[10px] md:text-xs font-medium">
              <span className={clsx('w-1.5 h-1.5 rounded-full', statusColor)} />
              <span className={hasError ? 'text-[var(--status-failed)]' : isPrinting ? 'text-[var(--status-completed)]' : online ? 'text-yellow-400' : 'text-[var(--brand-text-muted)]'}>{statusLabel}</span>
            </span>
            {hasCamera && (
              <button onClick={(e) => { e.stopPropagation(); onCameraClick(printer) }} className="p-1 hover:bg-[var(--brand-input-bg)] rounded-md transition-colors" aria-label="View camera">
                <Video size={14} className="text-[var(--brand-text-secondary)]" />
              </button>
            )}
          </div>
        </div>
        {activeJob && (
          <div className="mb-3 bg-[var(--brand-input-bg)] rounded-md p-3">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2 min-w-0">
                <Activity size={14} className="text-[var(--brand-primary)] animate-pulse flex-shrink-0" />
                <span className="text-sm font-medium truncate">{activeJob.job_name || 'Printing'}</span>
              </div>
              <div className="text-right flex-shrink-0">
                <span className="text-lg font-bold font-mono text-[var(--status-completed)]">{activeJob.progress_percent || 0}%</span>
              </div>
            </div>
            <ProgressBar value={activeJob.progress_percent || 0} color="green" size="md" className="mb-1.5" />
            <div className="flex justify-between text-xs text-[var(--brand-text-muted)]">
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
            <div key={idx} className="bg-[var(--brand-input-bg)] rounded-md p-1.5 md:p-2 text-center min-w-0 flex flex-col items-center gap-1">
              <SpoolRing
                color={slot.color_hex ? `#${slot.color_hex}` : '#888'}
                material=""
                level={slot.remaining != null ? slot.remaining : 100}
                empty={!slot.color_hex && !slot.material_type && !slot.type}
                size={20}
              />
              <span className="text-[10px] md:text-xs text-[var(--brand-text-muted)] truncate block">{getShortName(slot)}</span>
            </div>
          ))}
        </div>
      </div>
      {/* Bottom status bar */}
      <div className="px-3 md:px-4 py-2 md:py-3 bg-[var(--brand-content-bg)] border-t border-[var(--brand-card-border)]">
        <div className="flex items-center justify-between text-xs">
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1.5">
            <div className={`w-1.5 h-1.5 rounded-full ${statusColor}`}></div>
            <span className={online ? "text-[var(--status-completed)]" : "text-[var(--brand-text-muted)]"}>{statusLabel}</span>
          </div>
          {printer.lights_on != null && (
            <button
              onClick={(e) => {
                e.stopPropagation()
                printers.toggleLights(printer.id)
                  .then(() => toast.success(printer.lights_on ? 'Lights off' : 'Lights on'))
                  .catch(() => toast.error('Failed to toggle lights'))
              }}
              className={`p-0.5 rounded-md transition-colors ${printer.lights_on ? 'text-yellow-400 hover:text-yellow-300' : 'text-[var(--brand-text-muted)] hover:text-[var(--brand-text-secondary)]'}`}
              aria-label={printer.lights_on ? 'Turn lights off' : 'Turn lights on'}
            >
              <Lightbulb size={14} />
            </button>
          )}
        </div>
        <div className="flex items-center gap-3 text-[var(--brand-text-secondary)]">
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
            <span className="text-[var(--brand-primary)]">{stage}</span>
          )}
        </div>
        </div>
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
    <div className={clsx('bg-[var(--brand-card-bg)] rounded-md p-3 md:p-4 border-l-4 border border-[var(--brand-card-border)]', statusColors[job.status])}>
      <div className="flex items-center justify-between">
        <div className="min-w-0 mr-2">
          <h4 className="font-medium truncate">{job.item_name}</h4>
          <p className="text-sm text-[var(--brand-text-muted)] truncate">{job.printer?.name || 'Unassigned'} • {formatHours(job.duration_hours)}</p>
        </div>
        <div className="flex items-center gap-1.5 md:gap-2 flex-shrink-0">
          {canDo('dashboard.actions') && job.status === 'scheduled' && (
            <span className={clsx('px-2 py-1 rounded-md text-xs font-medium',
              job.status === 'scheduled' ? 'bg-blue-900/30 text-[var(--status-printing)]' :
              job.status === 'pending' ? 'bg-amber-900/30 text-amber-400' :
              'bg-[var(--brand-input-bg)] text-[var(--brand-text-secondary)]'
            )}>{job.status}</span>
          )}
          {canDo('dashboard.actions') && job.status === 'printing' && (
            <button onClick={() => onComplete(job.id)} className="p-1.5 md:p-2 bg-green-600 hover:bg-green-500 rounded-md transition-colors" aria-label="Mark job complete">
              <CheckCircle size={14} className="md:w-4 md:h-4" />
            </button>
          )}
          {canDo('dashboard.actions') && (job.status === 'scheduled' || job.status === 'printing' || job.status === 'pending') && (
            <button onClick={() => onCancel(job.id)} className="p-1.5 md:p-2 bg-[var(--brand-input-bg)] hover:bg-red-600 rounded-md transition-colors" aria-label="Cancel job">
              <XCircle size={14} className="md:w-4 md:h-4" />
            </button>
          )}
          <div className={clsx('status-dot', job.status)} />
        </div>
      </div>
      {job.colors_list?.length > 0 && (
        <div className="flex gap-1 mt-2">
          {job.colors_list.map((color, i) => (
            <div key={i} className="w-4 h-4 md:w-5 md:h-5 rounded-full border border-[var(--brand-card-border)]" style={{ backgroundColor: color }} role="img" aria-label={`Color: ${color}`} />
          ))}
        </div>
      )}
    </div>
  )
}

function MqttPrintItem({ job }) {
  return (
    <div className="bg-[var(--brand-card-bg)] rounded-md p-3 md:p-4 border-l-4 border border-[var(--brand-card-border)] border-l-[var(--brand-primary)]">
      <div className="flex items-center justify-between">
        <div className="min-w-0 mr-2">
          <h4 className="font-medium flex items-center gap-2 truncate">
            <Activity size={14} className="text-[var(--brand-primary)] animate-pulse flex-shrink-0" />
            <span className="truncate">{job.job_name || 'Unknown'}</span>
          </h4>
          <p className="text-sm text-[var(--brand-text-muted)] truncate">{job.printer_name} • Started {formatTime(job.started_at)}</p>
        </div>
        <div className="text-right flex-shrink-0">
          <p className="text-lg font-bold font-mono text-[var(--brand-primary)]">{job.progress_percent || 0}%</p>
        </div>
      </div>
    </div>
  )
}

function PrintHistoryItem({ job }) {
  const statusColors = {
    running: "border-yellow-500",
    completed: "border-[var(--brand-card-border)]",
    failed: "border-red-500",
    cancelled: "border-[var(--brand-card-border)]",
  }

  return (
    <div className={clsx('bg-[var(--brand-card-bg)] rounded-md p-3 md:p-4 border-l-4 border border-[var(--brand-card-border)]', statusColors[job.status] || 'border-l-[var(--brand-card-border)]')}>
      <div className="flex items-center justify-between">
        <div className="min-w-0 mr-2">
          <h4 className="font-medium truncate">{job.job_name || 'Unknown'}</h4>
          <p className="text-sm text-[var(--brand-text-muted)] truncate">{job.printer_name} • {formatTime(job.started_at)}</p>
        </div>
        <div className="text-right flex-shrink-0">
          <div className="font-medium text-[var(--brand-primary)]">{formatDuration(job.duration_minutes)}</div>
          {job.total_layers && <p className="text-xs text-[var(--brand-text-muted)]">{job.total_layers} layers</p>}
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
    { key: 'print_failed', count: summary.print_failed, icon: <XCircle size={14} className="text-[var(--status-failed)]" />, label: 'failed print', plural: 'failed prints', filter: 'critical' },
    { key: 'spool_low', count: summary.spool_low, icon: <AlertCircle size={14} className="text-amber-400" />, label: 'low spool', plural: 'low spools', filter: 'warning' },
    { key: 'maintenance_overdue', count: summary.maintenance_overdue, icon: <Wrench size={14} className="text-amber-400" />, label: 'maintenance overdue', plural: 'maintenance overdue', filter: 'warning' },
  ].filter(i => i.count > 0)

  return (
    <div className="mb-6 md:mb-8 rounded-md border border-amber-600/30 bg-amber-950/20 p-4">
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
            className="flex items-center gap-2 w-full text-left hover:bg-amber-900/20 rounded-md px-2 py-1.5 transition-colors"
          >
            <span className="flex-shrink-0">{item.icon}</span>
            <span className="text-sm text-amber-200">
              {item.count} {item.count === 1 ? item.label : item.plural}
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}


function MaintenanceWidget() {
  const navigate = useNavigate()
  const { data: maintData } = useQuery({
    queryKey: ['maintenance-status'],
    queryFn: maintenance.getStatus,
    refetchInterval: 60000,
  })

  if (!maintData) return null

  // Find tasks that are overdue or approaching (>75% progress)
  const needsAttention = []
  maintData.forEach(printer => {
    printer.tasks?.forEach(task => {
      if (task.status === 'overdue' || task.progress_percent >= 75) {
        needsAttention.push({
          printerId: printer.printer_id,
          printerName: printer.printer_name,
          taskName: task.task_name,
          status: task.status,
          progress: task.progress_percent,
          hoursSince: task.hours_since_service,
          daysSince: task.days_since_service,
        })
      }
    })
  })

  if (needsAttention.length === 0) return null

  return (
    <div>
      <h2 className="font-semibold text-sm text-[var(--brand-text-primary)] mb-4 flex items-center gap-2">
        <Wrench size={18} className="text-purple-400" />
        Maintenance Due
      </h2>
      <div className="space-y-2">
        {needsAttention.slice(0, 6).map((item, i) => {
          const isOverdue = item.status === 'overdue'
          return (
            <div key={i} className={`bg-[var(--brand-card-bg)] rounded-md border p-3 cursor-pointer hover:border-[var(--brand-card-border)] transition-colors ${isOverdue ? 'border-red-800' : 'border-[var(--brand-card-border)]'}`} onClick={() => navigate('/maintenance')}>
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-medium truncate">{item.printerName}</span>
                {isOverdue && <span className="text-xs bg-red-900/50 text-[var(--status-failed)] px-1.5 py-0.5 rounded-md font-medium">OVERDUE</span>}
              </div>
              <div className="text-xs text-[var(--brand-text-secondary)] mb-2">{item.taskName}</div>
              <ProgressBar value={Math.min(item.progress, 100)} color={isOverdue ? 'red' : item.progress >= 90 ? 'yellow' : 'blue'} size="sm" />
              <div className="text-xs text-[var(--brand-text-muted)] mt-1">
                {item.daysSince}d since service{item.hoursSince > 0 ? ` · ${item.hoursSince.toFixed(0)}h printed` : ''}
              </div>
            </div>
          )
        })}
        {needsAttention.length > 6 && (
          <div className="text-xs text-[var(--brand-text-muted)] text-center py-1">+{needsAttention.length - 6} more</div>
        )}
      </div>
    </div>
  )
}

export default function Dashboard() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [cameraTarget, setCameraTarget] = useState(null)
  const { data: activeCameras } = useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const response = await fetch('/api/cameras', { credentials: 'include' })
      if (!response.ok) return []
      return response.json()
    }
  })
  const cameraIds = new Set((activeCameras || []).map(c => c.id))

  const { data: statsData, isLoading: statsLoading } = useQuery({ queryKey: ['stats'], queryFn: () => stats.get() })
  const { data: dashAlertSummary } = useQuery({ queryKey: ['dash-alert-summary'], queryFn: alertsApi.summary, refetchInterval: 60000 })
  const { data: printersData, isLoading: printersLoading } = useQuery({ queryKey: ['printers'], queryFn: () => printers.list(true), refetchInterval: 30000 })
  const { data: activeJobs, isLoading: jobsLoading } = useQuery({ queryKey: ['jobs', 'active'], queryFn: () => jobs.list() })
  const { data: allPrintJobs } = useQuery({ queryKey: ['print-jobs'], queryFn: () => printJobs.list({ limit: 20 }), refetchInterval: 30000 })

  // Split MQTT jobs into running vs completed
  const runningMqttJobs = allPrintJobs?.filter(j => j.status === 'running') || []
  const completedMqttJobs = allPrintJobs?.filter(j => j.status !== 'running') || []

  const jobMutErr = (label: string) => (err: any) =>
    toast.error(`${label}: ${err?.message || 'Unknown error'}`)

  const startJob = useMutation({
    mutationFn: jobs.start,
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['jobs'] }); queryClient.invalidateQueries({ queryKey: ['stats'] }) },
    onError: jobMutErr('Start job failed'),
  })

  const completeJob = useMutation({
    mutationFn: jobs.complete,
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['jobs'] }); queryClient.invalidateQueries({ queryKey: ['stats'] }) },
    onError: jobMutErr('Complete job failed'),
  })

  const cancelJob = useMutation({
    mutationFn: jobs.cancel,
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['jobs'] }); queryClient.invalidateQueries({ queryKey: ['stats'] }) },
    onError: jobMutErr('Cancel job failed'),
  })

  // Calculate currently printing count (scheduled jobs + MQTT running)
  const currentlyPrinting = statsData?.jobs?.printing || 0

  // --- Task B: Change highlighting for stat cards ---
  const statValues = {
    printing: currentlyPrinting,
    scheduled: statsData?.jobs?.scheduled || 0,
    pending: statsData?.jobs?.pending || 0,
    completed_today: statsData?.jobs?.completed_today || 0,
    active_alerts: dashAlertSummary?.total || 0,
    maintenance_due: dashAlertSummary?.maintenance_overdue || 0,
  }

  const prevStatsRef = useRef(null)
  const [highlightedKeys, setHighlightedKeys] = useState(new Set())
  const highlightTimerRef = useRef(null)

  const computeChangedKeys = useCallback((prev, current) => {
    if (!prev) return new Set()
    const changed = new Set()
    for (const key of Object.keys(current)) {
      if (prev[key] !== current[key]) {
        changed.add(key)
      }
    }
    return changed
  }, [])

  useEffect(() => {
    const changed = computeChangedKeys(prevStatsRef.current, statValues)
    prevStatsRef.current = { ...statValues }

    if (changed.size > 0) {
      setHighlightedKeys(changed)
      if (highlightTimerRef.current) {
        clearTimeout(highlightTimerRef.current)
      }
      highlightTimerRef.current = setTimeout(() => {
        setHighlightedKeys(new Set())
      }, 1500)
    }

    return () => {
      if (highlightTimerRef.current) {
        clearTimeout(highlightTimerRef.current)
      }
    }
  }, [
    statValues.printing,
    statValues.scheduled,
    statValues.pending,
    statValues.completed_today,
    statValues.active_alerts,
    statValues.maintenance_due,
    computeChangedKeys,
  ])

  const highlightClass = (key) =>
    clsx(highlightedKeys.has(key) && 'ring-2 ring-[var(--brand-primary)]/50 transition-all duration-1000')

  const isLoading = statsLoading || printersLoading || jobsLoading

  if (isLoading) {
    return (
      <div className="p-4 md:p-6 flex items-center justify-center min-h-[50vh]">
        <Loader2 size={24} className="animate-spin text-[var(--brand-text-muted)]" />
      </div>
    )
  }

  return (
    <div className="p-4 md:p-6">
      <PageHeader icon={LayoutDashboard} title="Dashboard" subtitle="Print farm overview">
        <Button variant="secondary" size="md" icon={Monitor} onClick={() => navigate('/tv')}>
          TV Mode
        </Button>
      </PageHeader>

      <SectionErrorBoundary>
        <AlertsWidget />
      </SectionErrorBoundary>

      <SectionErrorBoundary>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2 md:gap-3 mb-6 md:mb-8">
          <StatCard label="Currently Printing" value={currentlyPrinting} icon={Play} color="green" onClick={() => navigate('/jobs?status=printing')} className={highlightClass('printing')} />
          <StatCard label="Scheduled" value={statValues.scheduled} icon={Clock} color="blue" onClick={() => navigate('/timeline')} className={highlightClass('scheduled')} />
          <StatCard label="Pending" value={statValues.pending} icon={AlertCircle} color="amber" onClick={() => navigate('/jobs?status=pending')} className={highlightClass('pending')} />
          <StatCard label="Completed Today" value={statValues.completed_today} icon={CheckCircle} color="default" className={highlightClass('completed_today')} />
          <StatCard label="Active Alerts" value={statValues.active_alerts} icon={AlertTriangle} color="red" onClick={() => navigate('/alerts')} className={highlightClass('active_alerts')} />
          <StatCard label="Maintenance Due" value={statValues.maintenance_due} icon={Activity} color="purple" onClick={() => navigate('/maintenance')} className={highlightClass('maintenance_due')} />
        </div>
      </SectionErrorBoundary>

      <div className="space-y-6 md:space-y-8">
        {/* Printers — full width */}
        <SectionErrorBoundary>
          <div>
            <h2 className="font-semibold text-sm text-[var(--brand-text-primary)] mb-4">Printers</h2>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
              {printersData?.map((printer) => (
                <PrinterCard
                  key={printer.id}
                  printer={printer}
                  hasCamera={cameraIds.has(printer.id)}
                  onCameraClick={setCameraTarget}
                  activeJob={runningMqttJobs.find(j => j.printer_id === printer.id)}
                  onClick={() => navigate('/printers')}
                />
              ))}
              {(!printersData || printersData.length === 0) && (
                <div className="col-span-full bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)]">
                  <EmptyState title="No printers configured." />
                </div>
              )}
            </div>
          </div>
        </SectionErrorBoundary>

        {/* Scheduled Jobs — full width */}
        <SectionErrorBoundary>
          <div>
            <div className="flex items-center justify-between mb-4">
              <h2 className="font-semibold text-sm text-[var(--brand-text-primary)]">Scheduled Jobs</h2>
              {activeJobs?.filter(j => ['scheduled', 'pending'].includes(j.status)).length > 12 && (
                <a href="/jobs" className="text-xs text-[var(--brand-primary)] hover:text-[var(--brand-primary)] transition-colors">View all →</a>
              )}
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {activeJobs?.filter(j => ['scheduled', 'pending'].includes(j.status))
                .slice(0, 12)
                .map((job) => (
                  <JobQueueItem
                    key={job.id}
                    job={job}
                    onStart={(id) => startJob.mutate(id)}
                    onComplete={(id) => completeJob.mutate(id)}
                    onCancel={(id) => cancelJob.mutate(id)}
                  />
                ))}
            </div>
            {(!activeJobs || activeJobs.filter(j => ['scheduled', 'pending'].includes(j.status)).length === 0) && (
              <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)]">
                <EmptyState title="No scheduled jobs" />
              </div>
            )}
          </div>
        </SectionErrorBoundary>

        {/* Recent Prints — full width */}
        <SectionErrorBoundary>
          <div>
            <h2 className="font-semibold text-sm text-[var(--brand-text-primary)] mb-4">Recent Prints</h2>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {completedMqttJobs.slice(0, 12).map((job) => <PrintHistoryItem key={job.id} job={job} />)}
            </div>
            {completedMqttJobs.length === 0 && (
              <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)]">
                <EmptyState title="No print history yet" />
              </div>
            )}
          </div>
        </SectionErrorBoundary>

        {/* Maintenance */}
        <SectionErrorBoundary>
          <MaintenanceWidget />
        </SectionErrorBoundary>
      </div>

      {cameraTarget && <CameraModal printer={cameraTarget} onClose={() => setCameraTarget(null)} />}
    </div>
  )
}
