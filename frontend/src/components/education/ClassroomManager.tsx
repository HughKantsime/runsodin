import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BookOpen, Link2, Link2Off, RefreshCw, Save, ShieldCheck, UsersRound } from 'lucide-react'
import { classroom } from '../../api'
import type { ClassroomCourse, EducationCostCenter } from '../../types'
import { Button, Card, EmptyState, Input, Modal, Select, Switch } from '../ui'

export default function ClassroomManager({ centers }: { centers: EducationCostCenter[] }) {
  const queryClient = useQueryClient()
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [domains, setDomains] = useState('')
  const [selectedCourse, setSelectedCourse] = useState<ClassroomCourse | null>(null)
  const [targetCenter, setTargetCenter] = useState('new')
  const [updateMetadata, setUpdateMetadata] = useState(false)
  const [disconnectOpen, setDisconnectOpen] = useState(false)
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)

  const statusQuery = useQuery({ queryKey: ['classroom-status'], queryFn: classroom.status })
  const status = statusQuery.data
  useEffect(() => {
    if (!status) return
    setClientId(status.client_id)
    setDomains(status.allowed_domains)
  }, [status])

  const coursesQuery = useQuery({
    queryKey: ['classroom-courses'],
    queryFn: classroom.courses,
    enabled: !!status?.connected,
  })
  const previewQuery = useQuery({
    queryKey: ['classroom-preview', selectedCourse?.id],
    queryFn: () => classroom.preview(selectedCourse!.id),
    enabled: !!status?.connected && !!selectedCourse,
  })
  const preview = previewQuery.data

  useEffect(() => {
    if (preview?.mapping) setTargetCenter(String(preview.mapping.cost_center_id))
    else setTargetCenter('new')
  }, [preview])

  const configure = useMutation({
    mutationFn: () => classroom.configure({
      client_id: clientId.trim(),
      allowed_domains: domains.trim(),
      ...(clientSecret ? { client_secret: clientSecret } : {}),
    }),
    onSuccess: (next) => {
      queryClient.setQueryData(['classroom-status'], next)
      setClientSecret('')
      setMessage({ type: 'success', text: 'Google Classroom OAuth settings saved.' })
    },
    onError: (error) => setMessage({ type: 'error', text: error instanceof Error ? error.message : 'Unable to save Classroom settings.' }),
  })

  const connect = useMutation({
    mutationFn: classroom.connectUrl,
    onSuccess: ({ authorization_url }) => window.location.assign(authorization_url),
    onError: (error) => setMessage({ type: 'error', text: error instanceof Error ? error.message : 'Unable to start Google authorization.' }),
  })

  const disconnectMutation = useMutation({
    mutationFn: classroom.disconnect,
    onSuccess: (next) => {
      queryClient.setQueryData(['classroom-status'], next)
      queryClient.removeQueries({ queryKey: ['classroom-courses'] })
      queryClient.removeQueries({ queryKey: ['classroom-preview'] })
      setSelectedCourse(null)
      setDisconnectOpen(false)
      setMessage({ type: 'success', text: 'Google authorization removed. Imported classes and grants were preserved.' })
    },
    onError: (error) => setMessage({ type: 'error', text: error instanceof Error ? error.message : 'Unable to disconnect Classroom.' }),
  })

  const importMutation = useMutation({
    mutationFn: () => classroom.importCourse(selectedCourse!.id, {
      ...(targetCenter !== 'new' ? { cost_center_id: Number(targetCenter) } : {}),
      update_metadata: updateMetadata,
      command_id: crypto.randomUUID(),
    }),
    onSuccess: (result) => {
      setMessage({
        type: 'success',
        text: `Imported ${result.teachers} teacher${result.teachers === 1 ? '' : 's'} and ${result.students} student${result.students === 1 ? '' : 's'}.`,
      })
      queryClient.invalidateQueries({ queryKey: ['classroom-preview'] })
      queryClient.invalidateQueries({ queryKey: ['education-centers'] })
    },
    onError: (error) => setMessage({ type: 'error', text: error instanceof Error ? error.message : 'Classroom roster import failed.' }),
  })

  const activeCenters = useMemo(() => centers.filter((center) => center.active), [centers])
  if (statusQuery.isLoading) return <p className="text-sm text-[var(--brand-text-secondary)]">Loading Google Classroom…</p>
  if (statusQuery.isError) {
    return <EmptyState icon={BookOpen} title="Classroom status unavailable" description="ODIN could not load the tenant Classroom connection." />
  }

  return (
    <div className="space-y-5">
      <Card className="border border-[var(--brand-card-border)]">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex items-start gap-3">
            <BookOpen size={20} className="mt-0.5 shrink-0 text-[var(--brand-primary)]" aria-hidden="true" />
            <div>
              <h2 className="text-sm font-semibold text-[var(--brand-text-primary)]">Google Classroom roster import</h2>
              <p className="mt-1 text-xs leading-relaxed text-[var(--brand-text-secondary)]">
                Separate read-only authorization for courses and rosters. ODIN never requests coursework, grades, or roster write access.
              </p>
              {status?.account_email && <p className="mt-2 text-xs text-[var(--brand-text-muted)]">Connected as {status.account_email}</p>}
            </div>
          </div>
          <span className={`inline-flex w-fit items-center gap-2 rounded-md border px-2.5 py-1 text-xs ${status?.connected ? 'border-[var(--status-completed)] text-[var(--status-completed)]' : 'border-[var(--brand-card-border)] text-[var(--brand-text-secondary)]'}`}>
            <span className={`h-1.5 w-1.5 rounded-full ${status?.connected ? 'bg-[var(--status-completed)]' : 'bg-[var(--status-pending)]'}`} />
            {status?.connected ? 'Connected' : status?.state === 'reconnect_required' ? 'Reconnect required' : 'Not connected'}
          </span>
        </div>
      </Card>

      <Card className="space-y-4 border border-[var(--brand-card-border)]">
        <div>
          <h3 className="text-sm font-semibold text-[var(--brand-text-primary)]">OAuth client</h3>
          <p className="mt-1 text-xs text-[var(--brand-text-secondary)]">Use a Google web OAuth client whose authorized redirect URI matches the value ODIN shows when connection begins.</p>
        </div>
        <Input label="Google OAuth client ID" value={clientId} onChange={(event) => setClientId(event.target.value)} className="font-mono" />
        <div className="grid gap-4 md:grid-cols-2">
          <Input
            label={status?.configured ? 'Client secret (configured)' : 'Client secret'}
            type="password"
            value={clientSecret}
            onChange={(event) => setClientSecret(event.target.value)}
            placeholder={status?.configured ? 'Leave blank to keep current secret' : 'Enter client secret'}
            autoComplete="new-password"
            className="font-mono"
          />
          <Input label="Allowed Workspace domains" value={domains} onChange={(event) => setDomains(event.target.value)} placeholder="ctechigh.org" className="font-mono" />
        </div>
        <div className="flex flex-col gap-3 sm:flex-row sm:justify-end">
          <Button variant="secondary" icon={Save} loading={configure.isPending} disabled={!clientId.trim() || !domains.trim() || (!status?.configured && !clientSecret)} onClick={() => configure.mutate()}>
            Save OAuth settings
          </Button>
          {status?.connected ? (
            <Button variant="danger" icon={Link2Off} onClick={() => setDisconnectOpen(true)}>Disconnect</Button>
          ) : (
            <Button icon={Link2} loading={connect.isPending} disabled={!status?.configured} onClick={() => connect.mutate()}>
              Authorize with Google
            </Button>
          )}
        </div>
      </Card>

      {message && (
        <div role={message.type === 'error' ? 'alert' : 'status'} className={`rounded-md border p-3 text-sm ${message.type === 'error' ? 'border-[var(--status-failed)] text-[var(--status-failed)]' : 'border-[var(--status-completed)] text-[var(--status-completed)]'}`}>
          {message.text}
        </div>
      )}

      {status?.connected && (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.4fr)]">
          <Card className="border border-[var(--brand-card-border)]">
            <div className="mb-4 flex items-center justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold text-[var(--brand-text-primary)]">Available courses</h3>
                <p className="mt-1 text-xs text-[var(--brand-text-secondary)]">Courses visible to the connected Google account.</p>
              </div>
              <Button variant="ghost" size="icon" icon={RefreshCw} aria-label="Refresh Classroom courses" loading={coursesQuery.isFetching} onClick={() => coursesQuery.refetch()} />
            </div>
            {coursesQuery.isLoading ? (
              <p className="text-xs text-[var(--brand-text-secondary)]">Loading courses…</p>
            ) : coursesQuery.isError ? (
              <p role="alert" className="text-xs text-[var(--status-failed)]">Unable to load courses. Reconnect Google Classroom and try again.</p>
            ) : coursesQuery.data?.items.length ? (
              <div className="space-y-2">
                {coursesQuery.data.items.map((course) => (
                  <button
                    key={course.id}
                    type="button"
                    onClick={() => setSelectedCourse(course)}
                    className={`w-full rounded-md border p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--brand-primary)] ${selectedCourse?.id === course.id ? 'border-[var(--brand-primary)] bg-[var(--brand-surface)]' : 'border-[var(--brand-card-border)] hover:bg-[var(--brand-surface)]'}`}
                  >
                    <span className="block text-sm font-medium text-[var(--brand-text-primary)]">{course.name}</span>
                    <span className="mt-1 block text-xs text-[var(--brand-text-secondary)]">{course.section || 'No section'}</span>
                  </button>
                ))}
              </div>
            ) : (
              <EmptyState icon={BookOpen} title="No active courses" description="The connected account cannot see any active Google Classroom courses." />
            )}
          </Card>

          <Card className="border border-[var(--brand-card-border)]">
            {!selectedCourse ? (
              <EmptyState icon={ShieldCheck} title="Select a course" description="Preview the full teacher and student roster before importing anything." />
            ) : previewQuery.isLoading ? (
              <p className="text-sm text-[var(--brand-text-secondary)]">Loading {selectedCourse.name} roster…</p>
            ) : previewQuery.isError || !preview ? (
              <EmptyState icon={UsersRound} title="Roster unavailable" description="ODIN could not load this roster. No users or grants were changed." />
            ) : (
              <div className="space-y-5">
                <div>
                  <h3 className="text-sm font-semibold text-[var(--brand-text-primary)]">{preview.course.name}</h3>
                  <p className="mt-1 text-xs text-[var(--brand-text-secondary)]">
                    {preview.teachers.length} teacher{preview.teachers.length === 1 ? '' : 's'} · {preview.students.length} student{preview.students.length === 1 ? '' : 's'}
                  </p>
                </div>

                <div className="grid grid-cols-3 gap-2 text-center">
                  <RosterStat label="Add/change" value={preview.diff.added_or_changed.length} />
                  <RosterStat label="Remove" value={preview.diff.removed.length} />
                  <RosterStat label="Unchanged" value={preview.diff.unchanged} />
                </div>

                <div className="grid gap-4 md:grid-cols-2">
                  <RosterList title="Teachers" members={preview.teachers} />
                  <RosterList title="Students" members={preview.students} />
                </div>

                <Select label="ODIN class or club" value={targetCenter} disabled={!!preview.mapping} onChange={(event) => setTargetCenter(event.target.value)}>
                  <option value="new">Create a new cost center from this course</option>
                  {activeCenters.map((center) => <option key={center.id} value={center.id}>{center.name} ({center.code})</option>)}
                </Select>
                {preview.mapping && <p className="text-xs text-[var(--brand-text-muted)]">This course is already mapped to cost center #{preview.mapping.cost_center_id}.</p>}
                <Switch label="Update class name and description" description="Roster sync never changes printer entitlements." checked={updateMetadata} onChange={(event) => setUpdateMetadata(event.target.checked)} />
                <Button fullWidth icon={UsersRound} loading={importMutation.isPending} onClick={() => importMutation.mutate()}>
                  {preview.mapping ? 'Sync roster' : 'Import course and roster'}
                </Button>
              </div>
            )}
          </Card>
        </div>
      )}

      <Modal isOpen={disconnectOpen} onClose={() => setDisconnectOpen(false)} title="Disconnect Google Classroom" size="sm" alert>
        <p className="text-sm leading-relaxed text-[var(--brand-text-secondary)]">ODIN will remove the retained Google authorization tokens. Existing users, cost centers, grants, submissions, and printer entitlements will remain.</p>
        <div className="mt-5 flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setDisconnectOpen(false)}>Cancel</Button>
          <Button variant="danger" loading={disconnectMutation.isPending} onClick={() => disconnectMutation.mutate()}>Disconnect</Button>
        </div>
      </Modal>
    </div>
  )
}

function RosterStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-md bg-[var(--brand-surface)] p-2">
      <div className="font-mono text-base font-semibold text-[var(--brand-text-primary)]">{value}</div>
      <div className="text-[11px] text-[var(--brand-text-secondary)]">{label}</div>
    </div>
  )
}

function RosterList({ title, members }: { title: string; members: Array<{ provider_user_id: string; name: string; email: string }> }) {
  return (
    <div>
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--brand-text-secondary)]">{title}</h4>
      <div className="max-h-64 space-y-1 overflow-y-auto rounded-md border border-[var(--brand-card-border)] p-2">
        {members.length ? members.map((member) => (
          <div key={member.provider_user_id} className="rounded-sm px-2 py-1.5 hover:bg-[var(--brand-surface)]">
            <div className="truncate text-xs font-medium text-[var(--brand-text-primary)]">{member.name}</div>
            <div className="truncate text-[11px] text-[var(--brand-text-muted)]">{member.email}</div>
          </div>
        )) : <p className="px-2 py-3 text-xs text-[var(--brand-text-muted)]">No members</p>}
      </div>
    </div>
  )
}
