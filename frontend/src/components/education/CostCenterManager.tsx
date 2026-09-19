import { useEffect, useMemo, useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Archive, Pencil, Plus, Save, Search, UsersRound } from 'lucide-react'
import toast from 'react-hot-toast'
import { education, printers, users } from '../../api'
import type { EducationCostCenter, User } from '../../types'
import ConfirmModal from '../shared/ConfirmModal'
import { Button, Card, EmptyState, Input, Modal, Select, Textarea } from '../ui'

interface CostCenterManagerProps {
  centers: EducationCostCenter[]
  includeArchived: boolean
  onIncludeArchivedChange: (value: boolean) => void
  loading: boolean
}

interface CenterDraft {
  name: string
  code: string
  description: string
}

const blankDraft: CenterDraft = { name: '', code: '', description: '' }

export default function CostCenterManager({
  centers,
  includeArchived,
  onIncludeArchivedChange,
  loading,
}: CostCenterManagerProps) {
  const queryClient = useQueryClient()
  const [selectedId, setSelectedId] = useState<number | null>(centers[0]?.id || null)
  const [editorOpen, setEditorOpen] = useState(false)
  const [editing, setEditing] = useState<EducationCostCenter | null>(null)
  const [draft, setDraft] = useState<CenterDraft>(blankDraft)
  const [search, setSearch] = useState('')
  const [grantDraft, setGrantDraft] = useState<Record<number, Set<'student' | 'manager'>>>({})
  const [printerDraft, setPrinterDraft] = useState<Set<number>>(new Set())
  const [confirmLifecycle, setConfirmLifecycle] = useState<EducationCostCenter | null>(null)

  useEffect(() => {
    if (selectedId && centers.some((center) => center.id === selectedId)) return
    setSelectedId(centers[0]?.id || null)
  }, [centers, selectedId])

  const selected = centers.find((center) => center.id === selectedId) || null
  const grants = useInfiniteQuery({
    queryKey: ['education-center-grants', selectedId],
    queryFn: ({ pageParam }) => education.listGrants(selectedId!, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor || undefined,
    enabled: !!selectedId,
  })
  const entitlements = useInfiniteQuery({
    queryKey: ['education-center-printers', selectedId],
    queryFn: ({ pageParam }) => education.listCenterPrinters(selectedId!, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor || undefined,
    enabled: !!selectedId,
  })
  const userQuery = useQuery({ queryKey: ['users'], queryFn: users.list })
  const printerQuery = useQuery({
    queryKey: ['printers', selected?.org_id],
    queryFn: () => printers.list(true, '', selected!.org_id),
    enabled: !!selected,
  })

  const grantRows = grants.data?.pages.flatMap((page) => page.items) || []
  const printerRows = entitlements.data?.pages.flatMap((page) => page.items) || []

  useEffect(() => {
    if (!selected || !grants.data || grants.hasNextPage) return
    const next: Record<number, Set<'student' | 'manager'>> = {}
    grantRows.forEach((grant) => {
      next[grant.user_id] ||= new Set()
      next[grant.user_id].add(grant.role)
    })
    setGrantDraft(next)
  }, [selected?.id, grants.data, grants.hasNextPage])

  useEffect(() => {
    if (!selected || !entitlements.data || entitlements.hasNextPage) return
    setPrinterDraft(new Set(printerRows.map((row) => row.printer_id)))
  }, [selected?.id, entitlements.data, entitlements.hasNextPage])

  useEffect(() => {
    if (grants.hasNextPage && !grants.isFetchingNextPage) grants.fetchNextPage()
  }, [grants.hasNextPage, grants.isFetchingNextPage])
  useEffect(() => {
    if (entitlements.hasNextPage && !entitlements.isFetchingNextPage) entitlements.fetchNextPage()
  }, [entitlements.hasNextPage, entitlements.isFetchingNextPage])

  const tenantUsers = useMemo(() => (userQuery.data || []).filter(
    (user) => selected && user.group_id === selected.org_id && user.is_active,
  ), [selected, userQuery.data])
  const filteredUsers = tenantUsers.filter((user) => {
    const needle = search.trim().toLowerCase()
    return !needle || user.username.toLowerCase().includes(needle) || user.email.toLowerCase().includes(needle)
  })
  const availablePrinters = (printerQuery.data || []).filter(
    (printer) => printer.org_id === selected?.org_id && printer.is_active && !printer.shared,
  )

  const refreshCenter = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['education-centers'] }),
      queryClient.invalidateQueries({ queryKey: ['education-center-grants', selectedId] }),
      queryClient.invalidateQueries({ queryKey: ['education-center-printers', selectedId] }),
      queryClient.invalidateQueries({ queryKey: ['education-submissions'] }),
    ])
  }

  const saveCenter = useMutation({
    mutationFn: () => editing
      ? education.updateCenter(editing.id, {
          revision: editing.revision,
          name: draft.name,
          code: draft.code,
          description: draft.description,
          command_id: crypto.randomUUID(),
        })
      : education.createCenter({ ...draft, command_id: crypto.randomUUID() }),
    onSuccess: async () => {
      toast.success(editing ? 'Cost center updated' : 'Cost center created')
      setEditorOpen(false)
      await refreshCenter()
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const saveRoster = useMutation({
    mutationFn: async () => {
      if (!selected) throw new Error('Select a cost center.')
      const grantsPayload = Object.entries(grantDraft)
        .map(([userId, roles]) => ({ user_id: Number(userId), roles: [...roles] }))
        .filter((item) => item.roles.length > 0)
      return education.replaceGrants(selected.id, {
        revision: selected.revision,
        grants: grantsPayload,
        command_id: crypto.randomUUID(),
      })
    },
    onSuccess: async () => {
      toast.success('Roster updated')
      await refreshCenter()
    },
    onError: (error: Error) => {
      toast.error(error.message === 'reload_required'
        ? 'This cost center changed. Reloaded the latest version.'
        : error.message)
      refreshCenter()
    },
  })
  const savePrinters = useMutation({
    mutationFn: () => {
      if (!selected) throw new Error('Select a cost center.')
      return education.replaceCenterPrinters(selected.id, {
        revision: selected.revision,
        printer_ids: [...printerDraft],
        command_id: crypto.randomUUID(),
      })
    },
    onSuccess: async () => {
      toast.success('Authorized printers updated')
      await refreshCenter()
    },
    onError: (error: Error) => {
      toast.error(error.message === 'reload_required'
        ? 'This cost center changed. Reloaded the latest version.'
        : error.message)
      refreshCenter()
    },
  })

  const lifecycle = useMutation({
    mutationFn: (center: EducationCostCenter) => education.setCenterLifecycle(
      center.id,
      center.active ? 'archive' : 'reopen',
      {
        revision: center.revision,
        reason: center.active ? 'Archived by tenant administrator' : 'Reopened by tenant administrator',
        command_id: crypto.randomUUID(),
      },
    ),
    onSuccess: async (_, center) => {
      toast.success(center.active ? 'Cost center archived' : 'Cost center reopened')
      setConfirmLifecycle(null)
      await refreshCenter()
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const toggleRole = (user: User, role: 'student' | 'manager') => {
    setGrantDraft((current) => {
      const next = { ...current }
      const roles = new Set(next[user.id] || [])
      if (roles.has(role)) roles.delete(role)
      else roles.add(role)
      next[user.id] = roles
      return next
    })
  }

  if (!loading && centers.length === 0) {
    return (
      <Card>
        <EmptyState
          icon={UsersRound}
          title="Create the first class or club"
          description="Cost centers connect students, teachers, and the printers they are allowed to use."
        >
          <Button icon={Plus} onClick={() => {
            setEditing(null)
            setDraft(blankDraft)
            setEditorOpen(true)
          }}>Create cost center</Button>
        </EmptyState>
        <CenterEditor
          open={editorOpen}
          draft={draft}
          editing={editing}
          pending={saveCenter.isPending}
          onDraftChange={setDraft}
          onClose={() => setEditorOpen(false)}
          onSave={() => saveCenter.mutate()}
        />
      </Card>
    )
  }

  return (
    <div className="grid gap-4 xl:grid-cols-[280px,minmax(0,1fr)]">
      <Card padding="sm" className="h-fit border border-[var(--brand-card-border)]">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-[var(--brand-text-primary)]">Classes and clubs</h2>
          <Button size="icon" aria-label="Create cost center" icon={Plus} onClick={() => {
            setEditing(null)
            setDraft(blankDraft)
            setEditorOpen(true)
          }} />
        </div>
        <label className="mb-3 flex items-center gap-2 text-xs text-[var(--brand-text-secondary)]">
          <input
            type="checkbox"
            checked={includeArchived}
            onChange={(event) => onIncludeArchivedChange(event.target.checked)}
          />
          Show archived
        </label>
        <div className="space-y-1">
          {centers.map((center) => (
            <button
              key={center.id}
              onClick={() => setSelectedId(center.id)}
              className={`w-full rounded-md px-3 py-2 text-left transition-colors ${
                selectedId === center.id
                  ? 'bg-[var(--brand-sidebar-active-bg)] text-[var(--brand-sidebar-active-text)]'
                  : 'text-[var(--brand-text-secondary)] hover:bg-[var(--brand-surface)]'
              }`}
            >
              <span className="block truncate text-sm font-medium">{center.name}</span>
              <span className="mt-0.5 block text-[10px] font-mono opacity-70">
                {center.code} · {center.counts.active_grants} people · {center.counts.active_printers} printers
              </span>
            </button>
          ))}
        </div>
      </Card>

      {selected && (
        <div className="space-y-4">
          <Card className="border border-[var(--brand-card-border)]">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="font-display text-lg font-semibold text-[var(--brand-text-primary)]">{selected.name}</h2>
                  <span className="rounded-sm bg-[var(--brand-surface)] px-2 py-0.5 font-mono text-xs text-[var(--brand-text-secondary)]">{selected.code}</span>
                  {!selected.active && <span className="text-xs text-[var(--status-pending)]">Archived</span>}
                </div>
                {selected.description && <p className="mt-2 text-sm text-[var(--brand-text-secondary)]">{selected.description}</p>}
              </div>
              <div className="flex gap-2">
                <Button size="sm" variant="secondary" icon={Pencil} onClick={() => {
                  setEditing(selected)
                  setDraft({ name: selected.name, code: selected.code, description: selected.description })
                  setEditorOpen(true)
                }}>Edit</Button>
                <Button size="sm" variant="ghost" icon={Archive} onClick={() => setConfirmLifecycle(selected)}>
                  {selected.active ? 'Archive' : 'Reopen'}
                </Button>
              </div>
            </div>
          </Card>

          <Card className="border border-[var(--brand-card-border)]">
            <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <h3 className="text-sm font-semibold text-[var(--brand-text-primary)]">Roster</h3>
                <p className="mt-1 text-xs text-[var(--brand-text-secondary)]">Student and manager access is specific to this class or club.</p>
              </div>
              <div className="relative sm:w-64">
                <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--brand-text-muted)]" />
                <Input aria-label="Search roster users" value={search} onChange={(event) => setSearch(event.target.value)} className="pl-9" placeholder="Search users" />
              </div>
            </div>
            <div className="max-h-80 space-y-1 overflow-y-auto pr-1">
              {filteredUsers.map((user) => (
                <div key={user.id} className="grid gap-2 rounded-md px-3 py-2 hover:bg-[var(--brand-surface)] sm:grid-cols-[minmax(0,1fr),auto,auto] sm:items-center">
                  <div className="min-w-0">
                    <p className="truncate text-sm text-[var(--brand-text-primary)]">{user.username}</p>
                    <p className="truncate text-xs text-[var(--brand-text-muted)]">{user.email}</p>
                  </div>
                  {(['student', 'manager'] as const).map((role) => (
                    <label key={role} className="flex min-h-9 items-center gap-2 rounded-md px-2 text-xs text-[var(--brand-text-secondary)]">
                      <input
                        type="checkbox"
                        checked={grantDraft[user.id]?.has(role) || false}
                        onChange={() => toggleRole(user, role)}
                      />
                      {role === 'student' ? 'Student' : 'Manager'}
                    </label>
                  ))}
                </div>
              ))}
            </div>
            <div className="mt-4 flex justify-end">
              <Button
                icon={Save}
                onClick={() => saveRoster.mutate()}
                loading={saveRoster.isPending}
                disabled={!selected.active || grants.hasNextPage || grants.isFetchingNextPage}
              >
                Save roster
              </Button>
            </div>
          </Card>

          <Card className="border border-[var(--brand-card-border)]">
            <h3 className="text-sm font-semibold text-[var(--brand-text-primary)]">Authorized printers</h3>
            <p className="mt-1 text-xs text-[var(--brand-text-secondary)]">Teachers can approve work only to selected active, non-shared printers.</p>
            <div className="mt-4 grid gap-2 sm:grid-cols-2">
              {availablePrinters.map((printer) => (
                <label key={printer.id} className="flex min-h-11 items-center gap-3 rounded-md border border-[var(--brand-card-border)] px-3 py-2 text-sm text-[var(--brand-text-secondary)]">
                  <input
                    type="checkbox"
                    checked={printerDraft.has(printer.id)}
                    onChange={() => setPrinterDraft((current) => {
                      const next = new Set(current)
                      if (next.has(printer.id)) next.delete(printer.id)
                      else next.add(printer.id)
                      return next
                    })}
                  />
                  <span className="min-w-0">
                    <span className="block truncate font-medium text-[var(--brand-text-primary)]">{printer.name}</span>
                    <span className="block truncate text-xs">{printer.machine_type || printer.model || printer.api_type}</span>
                  </span>
                </label>
              ))}
            </div>
            <div className="mt-4 flex justify-end">
              <Button
                icon={Save}
                onClick={() => savePrinters.mutate()}
                loading={savePrinters.isPending}
                disabled={!selected.active || entitlements.hasNextPage || entitlements.isFetchingNextPage}
              >
                Save printers
              </Button>
            </div>
          </Card>
        </div>
      )}

      <CenterEditor
        open={editorOpen}
        draft={draft}
        editing={editing}
        pending={saveCenter.isPending}
        onDraftChange={setDraft}
        onClose={() => setEditorOpen(false)}
        onSave={() => saveCenter.mutate()}
      />
      <ConfirmModal
        open={!!confirmLifecycle}
        title={confirmLifecycle?.active ? 'Archive cost center' : 'Reopen cost center'}
        message={confirmLifecycle?.active
          ? 'Archive this class or club? Active submissions must finish first.'
          : 'Reopen this class or club for new submissions?'}
        confirmText={confirmLifecycle?.active ? 'Archive' : 'Reopen'}
        onCancel={() => setConfirmLifecycle(null)}
        onConfirm={() => confirmLifecycle && lifecycle.mutate(confirmLifecycle)}
      />
    </div>
  )
}

function CenterEditor({
  open,
  draft,
  editing,
  pending,
  onDraftChange,
  onClose,
  onSave,
}: {
  open: boolean
  draft: CenterDraft
  editing: EducationCostCenter | null
  pending: boolean
  onDraftChange: (draft: CenterDraft) => void
  onClose: () => void
  onSave: () => void
}) {
  return (
    <Modal isOpen={open} onClose={onClose} title={editing ? 'Edit cost center' : 'Create cost center'}>
      <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); onSave() }}>
        <Input label="Name" value={draft.name} maxLength={200} required onChange={(event) => onDraftChange({ ...draft, name: event.target.value })} />
        <Input label="Code" value={draft.code} maxLength={100} required onChange={(event) => onDraftChange({ ...draft, code: event.target.value })} />
        <Textarea label="Description" value={draft.description} maxLength={2000} rows={4} onChange={(event) => onDraftChange({ ...draft, description: event.target.value })} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>Cancel</Button>
          <Button type="submit" loading={pending} disabled={!draft.name.trim() || !draft.code.trim()}>
            {editing ? 'Save changes' : 'Create cost center'}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
