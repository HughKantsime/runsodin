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
  const rows = [
    { icon: School, label: 'Education entitlement', detail: 'Signed Education workflow feature', ready: value.education_license },
    { icon: School, label: 'Education mode', detail: value.education_mode ? 'Enabled' : 'Enable in Settings', ready: value.education_mode },
    { icon: KeyRound, label: 'Single sign-on', detail: value.oidc.ready ? `${value.oidc.provider} is configured` : 'Identity provider setup required', ready: value.oidc.ready },
    { icon: Link2, label: 'Google Classroom', detail: value.classroom.connected ? `Connected as ${value.classroom.account_email}` : 'Optional; not connected', ready: value.classroom.connected, optional: true },
    { icon: UsersRound, label: 'Pilot roster', detail: `${value.pilot.student_grants} student and ${value.pilot.manager_grants} manager grants`, ready: value.pilot.student_grants > 0 && value.pilot.manager_grants > 0 },
    { icon: Printer, label: 'Authorized printers', detail: `${value.pilot.printer_entitlements} cost-center printer entitlements`, ready: value.pilot.printer_entitlements > 0 },
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
              <p className="mt-1 truncate text-[11px] text-[var(--brand-text-secondary)]">{detail}</p>
            </div>
          </div>
        ))}
      </div>
    </Card>
  )
}
