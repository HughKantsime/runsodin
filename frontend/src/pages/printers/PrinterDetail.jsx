import { useParams, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, Printer, Thermometer, Clock, Wifi, WifiOff, Activity } from 'lucide-react'
import { printers, printerTelemetry, printJobs } from '../../api'
import { isOnline } from '../../utils/shared'
import { PageHeader, Card, StatCard, StatusBadge, Button, ProgressBar } from '../../components/ui'


function formatDuration(seconds) {
  if (!seconds || seconds <= 0) return '--'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

function formatEta(minutes) {
  if (!minutes || minutes <= 0) return '--'
  const h = Math.floor(minutes / 60)
  const m = Math.round(minutes % 60)
  if (h > 0) return `${h}h ${m}m remaining`
  return `${m}m remaining`
}


export default function PrinterDetail() {
  const { id } = useParams()
  const navigate = useNavigate()

  const { data: printer, isLoading, error } = useQuery({
    queryKey: ['printer', id],
    queryFn: () => printers.get(id),
    refetchInterval: 5000,
  })

  const { data: telemetry } = useQuery({
    queryKey: ['printer-telemetry', id],
    queryFn: () => printerTelemetry.get(id, 1),
    refetchInterval: 10000,
    enabled: !!printer,
  })

  const { data: recentJobs } = useQuery({
    queryKey: ['printer-jobs', id],
    queryFn: () => printJobs.list({ printer_id: id, limit: 10 }),
    refetchInterval: 15000,
    enabled: !!printer,
  })

  if (isLoading) {
    return (
      <div className="p-4 md:p-6">
        <div className="flex items-center gap-3 mb-6">
          <div className="h-8 w-8 bg-[var(--brand-input-bg)] rounded-md animate-pulse" />
          <div className="h-6 w-48 bg-[var(--brand-input-bg)] rounded-md animate-pulse" />
        </div>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {[1, 2, 3].map(i => (
            <div key={i} className="h-24 bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md animate-pulse" />
          ))}
        </div>
      </div>
    )
  }

  if (error || !printer) {
    return (
      <div className="p-4 md:p-6">
        <Button variant="ghost" size="sm" icon={ArrowLeft} onClick={() => navigate('/printers')}>
          Back to Printers
        </Button>
        <Card className="mt-4 text-center py-12">
          <p className="text-sm" style={{ color: 'var(--brand-text-secondary)' }}>
            {error ? `Failed to load printer: ${error.message}` : 'Printer not found.'}
          </p>
        </Card>
      </div>
    )
  }

  const online = isOnline(printer)
  const progress = printer.progress ?? printer.print_progress ?? 0
  const currentJobName = printer.current_job || printer.job_name || null
  const nozzleTemp = printer.nozzle_temp ?? printer.temperatures?.nozzle ?? null
  const bedTemp = printer.bed_temp ?? printer.temperatures?.bed ?? null
  const etaMinutes = printer.remaining_time ?? printer.eta_minutes ?? null
  const jobList = Array.isArray(recentJobs) ? recentJobs : recentJobs?.items || []

  return (
    <div className="p-4 md:p-6 max-w-6xl mx-auto">
      {/* Back button */}
      <div className="mb-4">
        <Button variant="ghost" size="sm" icon={ArrowLeft} onClick={() => navigate('/printers')}>
          Back to Printers
        </Button>
      </div>

      {/* Header */}
      <PageHeader icon={Printer} title={printer.name} subtitle={`${printer.type || 'Printer'} ${printer.ip ? '@ ' + printer.ip : ''}`}>
        <StatusBadge status={online ? (currentJobName ? 'printing' : 'active') : 'error'} />
      </PageHeader>

      {/* Stats row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4 mb-6">
        <StatCard
          label="Status"
          value={online ? 'Online' : 'Offline'}
          icon={online ? Wifi : WifiOff}
          color={online ? 'green' : 'red'}
        />
        <StatCard
          label="Nozzle Temp"
          value={nozzleTemp != null ? `${Math.round(nozzleTemp)}\u00B0C` : '--'}
          icon={Thermometer}
          color={nozzleTemp > 200 ? 'amber' : 'blue'}
        />
        <StatCard
          label="Bed Temp"
          value={bedTemp != null ? `${Math.round(bedTemp)}\u00B0C` : '--'}
          icon={Thermometer}
          color={bedTemp > 50 ? 'amber' : 'blue'}
        />
        <StatCard
          label="ETA"
          value={etaMinutes ? formatEta(etaMinutes) : '--'}
          icon={Clock}
          color="purple"
        />
      </div>

      {/* Current Job */}
      <Card className="mb-6">
        <h2 className="text-sm font-medium mb-3" style={{ color: 'var(--brand-text-muted)' }}>
          Current Job
        </h2>
        {currentJobName ? (
          <div>
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-medium" style={{ color: 'var(--brand-text-primary)' }}>
                {currentJobName}
              </span>
              <span className="text-sm font-mono" style={{ color: 'var(--brand-text-muted)' }}>
                {Math.round(progress)}%
              </span>
            </div>
            <ProgressBar value={progress} color={progress >= 100 ? 'green' : 'print'} size="md" />
            {etaMinutes > 0 && (
              <p className="text-xs mt-2" style={{ color: 'var(--brand-text-muted)' }}>
                {formatEta(etaMinutes)}
              </p>
            )}
          </div>
        ) : (
          <p className="text-sm" style={{ color: 'var(--brand-text-muted)' }}>
            No job currently running.
          </p>
        )}
      </Card>

      {/* Telemetry */}
      {telemetry && Array.isArray(telemetry) && telemetry.length > 0 && (
        <Card className="mb-6">
          <h2 className="text-sm font-medium mb-3" style={{ color: 'var(--brand-text-muted)' }}>
            Recent Telemetry (last hour)
          </h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            {telemetry.slice(-1).map((point, i) => (
              <div key={i} className="space-y-1">
                {point.nozzle_temp != null && (
                  <div className="flex justify-between">
                    <span style={{ color: 'var(--brand-text-muted)' }}>Nozzle</span>
                    <span className="font-mono" style={{ color: 'var(--brand-text-primary)' }}>{Math.round(point.nozzle_temp)}{'\u00B0'}C</span>
                  </div>
                )}
                {point.bed_temp != null && (
                  <div className="flex justify-between">
                    <span style={{ color: 'var(--brand-text-muted)' }}>Bed</span>
                    <span className="font-mono" style={{ color: 'var(--brand-text-primary)' }}>{Math.round(point.bed_temp)}{'\u00B0'}C</span>
                  </div>
                )}
                {point.fan_speed != null && (
                  <div className="flex justify-between">
                    <span style={{ color: 'var(--brand-text-muted)' }}>Fan</span>
                    <span style={{ color: 'var(--brand-text-primary)' }}>{point.fan_speed}%</span>
                  </div>
                )}
                {point.speed != null && (
                  <div className="flex justify-between">
                    <span style={{ color: 'var(--brand-text-muted)' }}>Speed</span>
                    <span style={{ color: 'var(--brand-text-primary)' }}>{point.speed}%</span>
                  </div>
                )}
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* Printer Info */}
      <Card className="mb-6">
        <h2 className="text-sm font-medium mb-3" style={{ color: 'var(--brand-text-muted)' }}>
          Printer Details
        </h2>
        <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-2 text-sm">
          {printer.name && (
            <>
              <dt style={{ color: 'var(--brand-text-muted)' }}>Name</dt>
              <dd style={{ color: 'var(--brand-text-primary)' }}>{printer.name}</dd>
            </>
          )}
          {printer.type && (
            <>
              <dt style={{ color: 'var(--brand-text-muted)' }}>Type</dt>
              <dd style={{ color: 'var(--brand-text-primary)' }}>{printer.type}</dd>
            </>
          )}
          {printer.ip && (
            <>
              <dt style={{ color: 'var(--brand-text-muted)' }}>IP Address</dt>
              <dd className="font-mono" style={{ color: 'var(--brand-text-primary)' }}>{printer.ip}</dd>
            </>
          )}
          {printer.model && (
            <>
              <dt style={{ color: 'var(--brand-text-muted)' }}>Model</dt>
              <dd style={{ color: 'var(--brand-text-primary)' }}>{printer.model}</dd>
            </>
          )}
          {printer.serial && (
            <>
              <dt style={{ color: 'var(--brand-text-muted)' }}>Serial</dt>
              <dd className="font-mono" style={{ color: 'var(--brand-text-primary)' }}>{printer.serial}</dd>
            </>
          )}
          {printer.firmware_version && (
            <>
              <dt style={{ color: 'var(--brand-text-muted)' }}>Firmware</dt>
              <dd style={{ color: 'var(--brand-text-primary)' }}>{printer.firmware_version}</dd>
            </>
          )}
        </dl>
      </Card>

      {/* Recent Job History */}
      <Card>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-medium" style={{ color: 'var(--brand-text-muted)' }}>
            Recent Jobs
          </h2>
          <Activity size={16} style={{ color: 'var(--brand-text-muted)' }} />
        </div>
        {jobList.length > 0 ? (
          <div className="space-y-2">
            {jobList.slice(0, 10).map(job => (
              <div
                key={job.id}
                className="flex items-center justify-between py-2 border-b border-[var(--brand-card-border)] last:border-0"
              >
                <div className="flex-1 min-w-0">
                  <p className="text-sm truncate" style={{ color: 'var(--brand-text-primary)' }}>
                    {job.file_name || job.name || 'Untitled'}
                  </p>
                  <p className="text-xs" style={{ color: 'var(--brand-text-muted)' }}>
                    {job.started_at ? new Date(job.started_at).toLocaleDateString() : ''}
                    {job.duration ? ` \u2022 ${formatDuration(job.duration)}` : ''}
                  </p>
                </div>
                <StatusBadge status={job.status || 'pending'} size="sm" />
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm py-4 text-center" style={{ color: 'var(--brand-text-muted)' }}>
            No recent jobs for this printer.
          </p>
        )}
      </Card>
    </div>
  )
}
