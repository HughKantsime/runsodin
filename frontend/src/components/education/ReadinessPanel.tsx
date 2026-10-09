import { useQuery } from '@tanstack/react-query'
import { AlertCircle, CheckCircle2, DatabaseBackup, KeyRound, Link2, Printer, School, UsersRound } from 'lucide-react'
import { education } from '../../api'
import { Card, EmptyState } from '../ui'

export default function ReadinessPanel() {
  const query = useQuery({ queryKey: ['education-readiness'], queryFn: education.readiness })
  if (query.isLoading) return <p className="text-sm text-[var(--brand-text-secondary)]">Checking POC readiness…</p>
  if (!query.data || query.isError) {
    return <EmptyState icon={AlertCircle} title="Readiness unavailable" description="ODIN could not load the tenant readiness signals." />
  }
  const value = query.data
  const storage = value.storage
  const gib = (bytes: number | null) => ((bytes ?? 0) / 1024 ** 3).toFixed(2)
  const storageDetail = storage?.status === 'configuration_error'
    ? 'Upload storage configuration is invalid. Ask your administrator to correct EDUCATION_MIN_FREE_GIB: a finite value of at least 1 GiB is required. Uploads are blocked.'
    : !storage || storage.status === 'unknown'
    ? 'Storage could not be measured. Ask your administrator to check the Education upload volume and permissions.'
    : `${storage.administrator_override ? `Administrator minimum: ${gib(storage.configured_min_free_bytes)} GiB. ` : 'Default minimum: 10 GiB. '}${gib(storage.free_bytes)} GiB free of ${gib(storage.total_bytes)} GiB; ${gib(storage.reserve_bytes)} GiB reserve; ${gib(storage.upload_headroom_bytes)} GiB upload headroom. ${storage.status === 'blocked' ? 'Free space or expand the upload filesystem; confirm the volume mount.' : 'Headroom measured now; mount, write permissions, and account quotas are not verified. Each upload is checked again.'}${storage.upload_directory_exists === false ? ' Upload directory not yet created; measured its existing parent filesystem.' : ''}`
  const rows = [
    { icon: School, label: 'Education entitlement', detail: 'Signed Education workflow feature', ready: value.education_license },
    { icon: School, label: 'Education mode', detail: value.education_mode ? 'Enabled' : 'Enable in Settings', ready: value.education_mode },
    { icon: KeyRound, label: 'Single sign-on', detail: value.oidc.ready ? `${value.oidc.provider} is configured` : 'Identity provider setup required', ready: value.oidc.ready },
    { icon: Link2, label: 'Google Classroom', detail: value.classroom.connected ? `Connected as ${value.classroom.account_email}` : 'Optional; not connected', ready: value.classroom.connected, optional: true },
    { icon: UsersRound, label: 'Pilot class setup', detail: value.pilot.complete_centers > 0 ? `${value.pilot.complete_centers} active class(es) have an active student, manager, and authorized printer together. File compatibility and physical printing still need testing.` : 'Add an active student, manager, and authorized printer to the same active class in Classes & clubs.', ready: value.pilot.complete_centers > 0 },
    { icon: Printer, label: 'Education upload storage', detail: storageDetail, ready: storage?.status === 'ready' },
    { icon: DatabaseBackup, label: 'Backup workflow', detail: `Verified ${value.backup.database_backend} workflow available`, ready: value.backup.verified_workflow_available },
  ]
  return (
    <Card className="border border-[var(--brand-card-border)]">
      <div className="mb-4">
        <h2 className="text-sm font-semibold text-[var(--brand-text-primary)]">POC readiness</h2>
        <p className="mt-1 text-xs text-[var(--brand-text-secondary)]">Current configuration facts. This does not certify untested physical printer models.</p>
      </div>
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {rows.map(({ icon: Icon, label, detail, ready, optional }) => (
          <div key={label} className="flex items-start gap-3 rounded-md bg-[var(--brand-surface)] p-3">
            <Icon size={17} className="mt-0.5 shrink-0 text-[var(--brand-primary)]" aria-hidden="true" />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="text-xs font-medium text-[var(--brand-text-primary)]">{label}</span>
                <CheckCircle2 size={14} className={ready ? 'text-[var(--status-completed)]' : optional ? 'text-[var(--status-pending)]' : 'text-[var(--status-warning)]'} aria-label={ready ? 'Ready' : optional ? 'Optional' : 'Needs attention'} />
              </div>
              <p className="mt-1 break-words text-[11px] text-[var(--brand-text-secondary)]">{detail}</p>
            </div>
          </div>
        ))}
      </div>
    </Card>
  )
}
