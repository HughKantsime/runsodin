import { useFoundry } from '../../hooks/themePreferences'
import PageState from '../../components/ui/PageState'
import Table from '../../components/ui/Table'
import { displayName } from '../../utils/displayName'
import MaintenanceBanner from '../../components/printers/MaintenanceBanner'
import { useState, useCallback, useEffect, useMemo, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient, useInfiniteQuery } from '@tanstack/react-query'
import { Plus, Filter, ShoppingCart, Layers, Zap, RefreshCw, Clock, Briefcase } from 'lucide-react'
import { jobs, models, printers as printersApi, scheduler, getApprovalSetting, presets, bulkOps } from '../../api'
import { canDo } from '../../permissions'
import { useOrg } from '../../contexts/OrgContext'
import FailureReasonModal from '../../components/jobs/FailureReasonModal'
import { updateJobFailure } from '../../api'
import toast from 'react-hot-toast'
import ConfirmModal from '../../components/shared/ConfirmModal'
import JobRow, { JobCardList } from '../../components/jobs/JobRow'
import { useMediaQuery } from '../../hooks/useMediaQuery'
import ScrollTabs from '../../components/ui/ScrollTabs'
import TraceabilityPanel from '../../components/jobs/TraceabilityPanel'
import RecentlyCompleted from '../../components/jobs/RecentlyCompleted'
import { CreateJobModal, EditJobModal, RejectModal } from '../../components/jobs/JobModals'
import { statusOptions, statusOrder, queueCompare, statusesFor, orderFor } from '../../components/jobs/jobUtils'
import { useJobMutations } from '../../hooks/useJobMutations'
import JobTableHeader from '../../components/jobs/JobTableHeader'
import JobCorrectionModal from '../../components/jobs/JobCorrectionModal'
import { PageHeader, Button, SearchInput, TabBar } from '../../components/ui'


const jobTypeTabs = [
  { value: 'all', label: 'All Jobs', icon: Layers },
  { value: 'approval', label: 'Awaiting Approval', icon: Clock },
  { value: 'order', label: 'Order Jobs', icon: ShoppingCart },
  { value: 'adhoc', label: 'Ad-hoc', icon: null },
]

const BULK_BATCH = 100 // POST /jobs/bulk-update refuses more

export default function Jobs() {
  const foundry = useFoundry()
  const compact = useMediaQuery('(max-width: 639px)')
  const org = useOrg()
  const queryClient = useQueryClient()
  const {
    createJob, updateJob, startJob, completeJob, cancelJob, failJob,
    deleteJob, repeatJob, dispatchJob, dispatchFencedIds, approveJobMut, rejectJobMut, resubmitJobMut,
  } = useJobMutations()

  const runScheduler = useMutation({
    mutationFn: scheduler.run,
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['jobs'] }); queryClient.invalidateQueries({ queryKey: ['stats'] }); toast.success('Scheduler run complete') },
    onError: (err) => toast.error('Scheduler failed: ' + (err.message || 'Unknown error')),
  })

  const [searchParams, setSearchParams] = useSearchParams()

  const [showCreateModal, setShowCreateModal] = useState(false)
  const [statusFilter, _setStatusFilter] = useState(() => searchParams.get('status') || '')
  const [searchQuery, _setSearchQuery] = useState(() => searchParams.get('q') || '')
  // Default 'queue' order = server queue order: priority, then drag-and-drop position
  // within the priority (unplaced last), then age.
  const [sortField, setSortField] = useState('queue')
  const [sortDirection, setSortDirection] = useState('asc')
  const [jobTypeFilter, _setJobTypeFilter] = useState(() => searchParams.get('type') || 'all')

  const updateSearchParams = useCallback((updates: Record<string, string>) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      Object.entries(updates).forEach(([key, value]) => {
        if (value && value !== '' && (key !== 'type' || value !== 'all')) {
          next.set(key, value)
        } else {
          next.delete(key)
        }
      })
      return next
    }, { replace: true })
  }, [setSearchParams])

  const setStatusFilter = useCallback((value) => {
    _setStatusFilter(value)
    updateSearchParams({ status: value })
  }, [updateSearchParams])

  const setSearchQuery = useCallback((value) => {
    _setSearchQuery(value)
    updateSearchParams({ q: value })
  }, [updateSearchParams])

  const setJobTypeFilter = useCallback((value) => {
    _setJobTypeFilter(value)
    updateSearchParams({ type: value })
  }, [updateSearchParams])

  // The box types freely; the server is asked (and the URL updated) once typing pauses.
  const [searchInput, setSearchInput] = useState(searchQuery)
  const pushedSearch = useRef(searchQuery)
  // A search set from elsewhere (the URL, a reset) replaces what is in the box...
  useEffect(() => {
    if (searchQuery !== pushedSearch.current) { pushedSearch.current = searchQuery; setSearchInput(searchQuery) }
  }, [searchQuery])
  // ...and typing is the only thing that pushes the box into the search.
  useEffect(() => {
    const next = searchInput.trim()
    if (next === pushedSearch.current) return
    const timer = setTimeout(() => { pushedSearch.current = next; setSearchQuery(next) }, 300)
    return () => clearTimeout(timer)
  }, [searchInput, setSearchQuery])

  const [printerFilter, _setPrinterFilter] = useState(() => searchParams.get('printer') || '')
  const [heldOnly, _setHeldOnly] = useState(() => searchParams.get('held') === '1')
  const setPrinterFilter = useCallback((value: string) => {
    _setPrinterFilter(value)
    updateSearchParams({ printer: value })
  }, [updateSearchParams])
  const setHeldOnly = useCallback((value: boolean) => {
    _setHeldOnly(value)
    updateSearchParams({ held: value ? '1' : '' })
  }, [updateSearchParams])

  // /jobs?job=218 and /jobs/218 both name a job to open.
  const deepLinkParam = searchParams.get('job') ?? ''
  const deepLinkId = /^[1-9]\d{0,14}$/.test(deepLinkParam) ? Number(deepLinkParam) : null
  const [showRejectModal, setShowRejectModal] = useState(false)
  const [rejectingJobId, setRejectingJobId] = useState(null)
  const [failureModal, setFailureModal] = useState(null)
  const [editingJob, setEditingJob] = useState(null)
  const [correctingJob, setCorrectingJob] = useState(null)
  const closeCorrection = useCallback(() => setCorrectingJob(null), [])
  const [selectedJobs, setSelectedJobs] = useState(new Set<number>())
  const [confirmAction, setConfirmAction] = useState(null)
  const [draggedId, setDraggedId] = useState(null)
  const [dragOverId, setDragOverId] = useState(null)

  const toggleJobSelect = (id) => setSelectedJobs(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n })
  // Select all is judged by membership, not by count: the rows on screen (a linked job's extra
  // row included) are all selected, or they are not.
  const toggleSelectAll = (jobIds) => setSelectedJobs(prev => jobIds.length > 0 && jobIds.every(id => prev.has(id)) ? new Set() : new Set(jobIds))

  const bulkAction = useMutation({
    // The server takes at most 100 jobs per request and the Active view can hold more, so a
    // large selection goes in batches; counts from every batch are added up.
    mutationFn: async ({ action, extra }: { action: string; extra?: Record<string, unknown> }) => {
      const ids = [...selectedJobs]
      if (ids.length <= BULK_BATCH) return bulkOps.jobs(ids, action, extra)
      let affected = 0, skipped = 0, counted = true
      for (let i = 0; i < ids.length; i += BULK_BATCH) {
        const part = (await bulkOps.jobs(ids.slice(i, i + BULK_BATCH), action, extra) ?? {}) as { affected?: number; skipped?: number }
        if (Number.isInteger(part.affected) && Number.isInteger(part.skipped)) { affected += part.affected as number; skipped += part.skipped as number }
        else counted = false
      }
      return counted ? { affected, skipped } : {}
    },
    onSuccess: (result, vars) => {
      queryClient.invalidateQueries({ queryKey: ['jobs'] }); setSelectedJobs(new Set())
      if (vars.action === 'reschedule') {
        const counts = (result ?? {}) as { affected?: number; skipped?: number }
        if (Number.isInteger(counts.affected) && (counts.affected ?? -1) >= 0 && Number.isInteger(counts.skipped) && (counts.skipped ?? -1) >= 0) toast.success(`${counts.affected} job(s) returned to queue; ${counts.skipped} unchanged`)
        else toast.error('Reschedule result unavailable. Refresh the queue.')
      } else toast.success(`Bulk ${vars.action} completed`)
    },
    // An earlier batch may already have been applied: show the real state.
    onError: (err, vars) => { queryClient.invalidateQueries({ queryKey: ['jobs'] }); toast.error(`Bulk ${vars.action} failed: ${err.message}`) },
  })

  const schedulePreset = useMutation({
    mutationFn: presets.schedule,
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['jobs'] }); toast.success('Preset scheduled') },
    onError: (err) => toast.error('Schedule preset failed: ' + err.message),
  })
  const deletePreset = useMutation({
    mutationFn: presets.delete,
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['presets'] }); toast.success('Preset deleted') },
    onError: (err) => toast.error('Delete preset failed: ' + err.message),
  })
  const createPreset = useMutation({
    mutationFn: presets.create,
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['presets'] }); toast.success('Preset saved') },
    onError: (err) => toast.error('Save preset failed: ' + err.message),
  })

  const { data: approvalSetting } = useQuery({ queryKey: ['approval-setting'], queryFn: getApprovalSetting })
  const { data: recentPrincipal } = useQuery<{ id: number; group_id: number | null; role: string }>({ queryKey: ['auth', 'me'], enabled: false })
  const recentReady = Number.isInteger(recentPrincipal?.id) && (recentPrincipal?.id ?? 0) > 0
  const { data: recentJobs, isLoading: recentLoading, isError: recentError, refetch: retryRecent } = useQuery({ queryKey: ['jobs', 'recent-terminal', recentPrincipal?.id ?? null, recentPrincipal?.group_id ?? null, recentPrincipal?.role ?? null, org.orgId], enabled: recentReady, queryFn: () => jobs.recentTerminal(org.orgId) })
  // The server does the filtering and the searching: the browser never sees a silently cut-off list.
  const requestedStatuses = statusesFor(statusFilter)
  const serverOrder = orderFor(statusFilter)
  // The queue loads whole (the server's largest page); history pages 50 at a time.
  const pageSize = serverOrder === 'queue' ? 500 : 50
  const printerParam = /^\d+$/.test(printerFilter) ? Number(printerFilter) : null
  const listParams = { order: serverOrder, q: searchQuery, printerId: printerParam, unassigned: printerFilter === 'unassigned', held: heldOnly }
  const jobsQuery = useInfiniteQuery({
    queryKey: ['jobs', 'list', statusFilter, org.orgId, searchQuery, printerFilter, heldOnly],
    initialPageParam: 0,
    queryFn: async ({ pageParam }) => {
      const box: { total: number | null } = { total: null }
      const items = await jobs.list(requestedStatuses, org.orgId, { ...listParams, limit: pageSize, offset: pageParam }, value => { box.total = value })
      return { items: items ?? [], total: box.total }
    },
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((sum, page) => sum + page.items.length, 0)
      if (last.items.length === 0) return undefined
      if (last.total != null) return loaded < last.total ? loaded : undefined
      return last.items.length >= pageSize ? loaded : undefined
    },
  })
  const isLoading = jobsQuery.isLoading
  const jobsData = useMemo(() => {
    if (!jobsQuery.data) return undefined
    const seen = new Set<number>()
    return jobsQuery.data.pages.flatMap(page => page.items).filter(job => !seen.has(job.id) && seen.add(job.id))
  }, [jobsQuery.data])
  const jobsTotal = jobsQuery.data?.pages[jobsQuery.data.pages.length - 1]?.total ?? null
  // A selection must not survive a change of what is on screen.
  useEffect(() => { setSelectedJobs(new Set()) }, [statusFilter, searchQuery, printerFilter, heldOnly, org.orgId])

  // Deep link: the job is opened even when the current filter does not list it.
  const onTab = (job: { status: string; order_item_id?: number | null }) =>
    jobTypeFilter === 'approval' ? job.status === 'submitted'
      : jobTypeFilter === 'order' ? job.order_item_id != null
        : jobTypeFilter === 'adhoc' ? job.order_item_id == null : true
  // "Listed" means drawn: loaded AND on the current tab; otherwise the job is fetched and shown on its own row.
  const deepListed = deepLinkId != null && (jobsData || []).some(job => job.id === deepLinkId && onTab(job))
  const { data: deepFetched, isError: deepMissing } = useQuery({
    queryKey: ['jobs', 'detail', deepLinkId, org.orgId],
    queryFn: () => jobs.get(deepLinkId as number),
    enabled: deepLinkId != null && jobsData !== undefined && !deepListed,
    retry: false,
  })
  const deepJob = deepLinkId == null ? null : ((jobsData || []).find(job => job.id === deepLinkId) ?? deepFetched ?? null)
  const deepExtra = deepFetched && !deepListed ? deepFetched : null
  const openEditor = (job) => {
    const actor = recentPrincipal
    if (actor?.role === 'viewer' && Number.isInteger(actor.id) && actor.id > 0 && Number.isInteger(actor.group_id) && actor.group_id > 0 && actor.group_id === job.charged_to_org_id && actor.id === job.submitted_by && job.status === 'rejected') setCorrectingJob({ job, actor, orgId: org.orgId })
    else if (['operator','admin'].includes(actor?.role) && canDo('jobs.edit')) setEditingJob(job)
  }
  const openedDeepLink = useRef<number | null>(null)
  // No dependency list on purpose: it runs after every render and the ref makes it fire once per link.
  useEffect(() => {
    if (deepLinkId == null) { openedDeepLink.current = null; return }
    if (!deepJob || !recentPrincipal || openedDeepLink.current === deepLinkId) return
    openedDeepLink.current = deepLinkId
    document.querySelector(`[data-job-id="${deepLinkId}"]`)?.scrollIntoView?.({ block: 'center' })
    if (['pending', 'scheduled', 'submitted', 'rejected'].includes(deepJob.status)) openEditor(deepJob)
  })
  useEffect(() => {
    if (!correctingJob) return
    const current = jobsData?.find(job => job.id === correctingJob.job.id) ?? (deepJob?.id === correctingJob.job.id ? deepJob : undefined)
    if (org.orgId !== correctingJob.orgId || jobsData && (!current || current.status !== 'rejected' || current.submitted_by !== correctingJob.actor.id)) closeCorrection()
  }, [jobsData, deepJob, org.orgId, correctingJob, closeCorrection])
  const { data: modelsData } = useQuery({ queryKey: ['models', org.orgId], queryFn: () => models.list(org.orgId) })
  const { data: printersData } = useQuery({ queryKey: ['printers'], queryFn: () => printersApi.list() })
  const { data: presetsData } = useQuery({ queryKey: ['presets'], queryFn: () => presets.list() })

  const handleDragStart = (e, jobId) => { if (!canDo('jobs.edit')) { e.preventDefault(); return }; setDraggedId(jobId); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', jobId); e.currentTarget.style.opacity = '0.4' }
  const handleDragEnd = (e) => { e.currentTarget.style.opacity = '1'; setDraggedId(null); setDragOverId(null) }
  const handleDragOver = (e, jobId) => { if (!canDo('jobs.edit')) return; e.preventDefault(); e.dataTransfer.dropEffect = 'move'; if (jobId !== draggedId) setDragOverId(jobId) }
  const handleDrop = async (e, targetId) => {
    e.preventDefault(); setDragOverId(null); setDraggedId(null)
    if (!canDo('jobs.edit') || !draggedId || draggedId === targetId) return
    const currentJobs = (jobsData || []).filter(j => j.status === 'pending' || j.status === 'scheduled')
    const fromIdx = currentJobs.findIndex(j => j.id === draggedId)
    const toIdx = currentJobs.findIndex(j => j.id === targetId)
    if (fromIdx === -1 || toIdx === -1) return
    if ((currentJobs[fromIdx].priority ?? 99) !== (currentJobs[toIdx].priority ?? 99)) {
      toast.error('Drag reorders jobs within the same priority; change the priority to move it further')
      return
    }
    const reordered = [...currentJobs]; const [moved] = reordered.splice(fromIdx, 1); reordered.splice(toIdx, 0, moved)
    try { await jobs.reorder(reordered.map(j => j.id)); queryClient.invalidateQueries({ queryKey: ['jobs'] }) }
    catch (err) { toast.error('Reorder failed: ' + (err.message || 'Unknown error')) }
  }

  const handleAction = (action, jobId, jobName, existingReason, existingNotes) => {
    switch (action) {
      case 'start':
        setConfirmAction({ action: 'start', jobId, title: 'Start Job', message: 'Start printing this job?', confirmText: 'Start', confirmVariant: 'confirm' })
        break
      case 'complete':
        setConfirmAction({ action: 'complete', jobId, title: 'Complete Job', message: 'Mark this job as completed?', confirmText: 'Complete', confirmVariant: 'confirm' })
        break
      case 'cancel':
        setConfirmAction({ action: 'cancel', jobId, title: 'Cancel Job', message: 'Cancel this job? If printing, the printer will NOT be stopped automatically.', confirmText: 'Cancel Job', confirmVariant: 'danger' })
        break
      case 'repeat': repeatJob.mutate(jobId); break
      case 'delete':
        setConfirmAction({ action: 'delete', jobId, title: 'Delete Job', message: 'Permanently delete this job? This cannot be undone.', confirmText: 'Delete', confirmVariant: 'danger' })
        break
      case 'edit': {
        const job = (jobsData || []).find(j => j.id === jobId) ?? (deepJob?.id === jobId ? deepJob : null)
        if (job) openEditor(job)
        break
      }
      case 'dispatch': {
        const printerLabel = existingReason || 'the printer'
        setConfirmAction({
          action: 'dispatch',
          jobId,
          title: 'Dispatch to Printer',
          message: `"${jobName}" will be uploaded to ${printerLabel} via FTP and the print will start automatically.\n\nMake sure the bed is clear before confirming.`,
          confirmText: 'Dispatch',
          confirmVariant: 'confirm',
        })
        break
      }
      case 'approve': approveJobMut.mutate(jobId); break
      case 'reject': setRejectingJobId(jobId); setShowRejectModal(true); break
      case 'resubmit': resubmitJobMut.mutate(jobId); break
      case 'markFailed':
        // Mark the printing job failed (POST /jobs/{id}/fail), then log the reason.
        failJob.mutate(jobId, {
          onSuccess: () => setFailureModal({ jobId, jobName })
        })
        break
      case 'failReason':
        setFailureModal({ jobId, jobName, existingReason, existingNotes })
        break
    }
  }

  const handleConfirmAction = () => {
    if (!confirmAction) return
    const { action, jobId } = confirmAction
    if (action === 'start') startJob.mutate(jobId)
    else if (action === 'complete') completeJob.mutate(jobId)
    else if (action === 'cancel') cancelJob.mutate(jobId)
    else if (action === 'delete') deleteJob.mutate(jobId)
    else if (action === 'dispatch') dispatchJob.mutate(jobId)
    else if (action === 'bulkDelete') bulkAction.mutate({ action: 'delete' })
    setConfirmAction(null)
  }

  const handleFailureSubmit = async (jobId, reason, notes) => {
    await updateJobFailure(jobId, reason, notes)
    queryClient.invalidateQueries({ queryKey: ['jobs'] })
    queryClient.invalidateQueries({ queryKey: ['print-jobs'] })
  }

  const toggleSort = (field) => {
    if (sortField === field) {
      setSortDirection(prev => prev === 'asc' ? 'desc' : 'asc')
    } else {
      setSortField(field)
      setSortDirection('asc')
    }
  }

  // Filter by job type (all, order, adhoc, approval)
  const typeFilteredJobs = (jobsData || []).filter(onTab)

  // Then filter by search and sort
  // Search already ran on the server; only the sort is local (and keeps the server's order for history).
  const filteredJobs = typeFilteredJobs
    .sort((a, b) => {
      const dir = sortDirection === 'asc' ? 1 : -1
      switch (sortField) {
        case 'status': return dir * ((statusOrder[a.status] ?? 99) - (statusOrder[b.status] ?? 99))
        case 'item_name': return dir * displayName(a).localeCompare(displayName(b))
        case 'queue': return serverOrder === 'queue' ? dir * queueCompare(a, b) : 0
        case 'priority': return dir * ((a.priority ?? 99) - (b.priority ?? 99))
        case 'printer': return dir * ((a.printer?.name || '').localeCompare(b.printer?.name || ''))
        case 'duration_hours': return dir * ((a.duration_hours || 0) - (b.duration_hours || 0))
        case 'scheduled_start': return dir * (new Date(a.scheduled_start || 0).getTime() - new Date(b.scheduled_start || 0).getTime())
        default: return 0
      }
    })

  // Count jobs by type for badge display
  const approvalJobCount = (jobsData || []).filter(j => j.status === 'submitted').length
  const orderJobCount = (jobsData || []).filter(j => j.order_item_id != null).length
  const adhocJobCount = (jobsData || []).filter(j => j.order_item_id == null).length

  const selectableIds = [...(deepExtra ? [deepExtra.id] : []), ...filteredJobs.map(j => j.id)]
  return (
    <div className="p-4 md:p-6">
      <PageHeader icon={Briefcase} title="Jobs" subtitle="Manage print queue">
        {canDo('jobs.create') && (
          <Button
            variant="secondary"
            icon={Zap}
            onClick={() => runScheduler.mutate()}
            disabled={runScheduler.isPending}
            loading={runScheduler.isPending}
            aria-busy={runScheduler.isPending}
          >
            {runScheduler.isPending ? 'Running...' : 'Run Scheduler'}
          </Button>
        )}
        {canDo('jobs.create') && (
          <Button variant="primary" icon={Plus} onClick={() => setShowCreateModal(true)}>
            New Job
          </Button>
        )}
      </PageHeader>
      <TraceabilityPanel />

      {/* Job Type Tabs */}
      <ScrollTabs className="mb-4 max-sm:overflow-x-auto" fadeClassName="from-[var(--brand-card-bg)]">
        <TabBar
          tabs={jobTypeTabs.map(tab => ({
            ...tab,
            count: tab.value === 'all' ? (jobsData?.length || 0) :
                   tab.value === 'approval' ? approvalJobCount :
                   tab.value === 'order' ? orderJobCount : adhocJobCount,
          }))}
          active={jobTypeFilter}
          onChange={setJobTypeFilter}
        />
      </ScrollTabs>

      {/* Quick Schedule from Presets */}
      {presetsData?.length > 0 && (
        <div className="mb-4 bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md p-3">
          <div className="flex items-center gap-2 mb-2">
            <Zap size={14} className="text-amber-400" />
            <span className="text-xs font-medium text-[var(--brand-text-secondary)]">Quick Schedule</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {presetsData.map(p => (
              <div key={p.id} className="flex items-center gap-1.5 bg-[var(--brand-input-bg)] rounded-md px-3 py-1.5 border border-[var(--brand-card-border)]">
                <button
                  onClick={() => schedulePreset.mutate(p.id)}
                  className="text-sm text-[var(--brand-text-primary)] hover:text-white transition-colors"
                  title={`Schedule: ${p.item_name || p.name}`}
                >
                  {p.name}
                </button>
                {canDo('jobs.delete') && (
                  <button onClick={() => deletePreset.mutate(p.id)} className="text-[var(--brand-text-muted)] hover:text-red-400 ml-1" title="Delete preset">&times;</button>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3 mb-4 md:mb-6">
        <SearchInput
          value={searchInput}
          onChange={(e) => setSearchInput(e.target.value)}
          placeholder="Search jobs..."
          className="flex-1 sm:max-w-md"
        />
        <div className="flex items-center gap-2">
          <Filter size={16} className="text-[var(--brand-text-muted)]" />
          <select aria-label="Filter jobs by status" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm">
            {statusOptions.map(opt => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>
        <select aria-label="Filter jobs by printer" value={printerFilter} onChange={(e) => setPrinterFilter(e.target.value)} className="bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm">
          <option value="">All printers</option>
          <option value="unassigned">Unassigned</option>
          {(printersData || []).map(p => (
            <option key={p.id} value={String(p.id)}>{p.name}</option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-sm text-[var(--brand-text-secondary)]">
          <input type="checkbox" checked={heldOnly} onChange={(e) => setHeldOnly(e.target.checked)} className="h-4 w-4 rounded" />
          Held only
        </label>
      </div>

      {selectedJobs.size > 0 && (
        <div className="flex items-center gap-3 mb-3 p-3 bg-[var(--brand-primary)]/10 border border-[var(--brand-primary)]/30 rounded-md">
          <span className="text-sm text-[var(--brand-text-secondary)]">{selectedJobs.size} selected</span>
          <Button variant="tertiary" size="sm" onClick={() => bulkAction.mutate({ action: 'cancel' })} disabled={bulkAction.isPending}>Cancel</Button>
          <Button variant="tertiary" size="sm" onClick={() => bulkAction.mutate({ action: 'set_priority', extra: { priority: 1 } })} disabled={bulkAction.isPending}>Priority 1</Button>
          <Button variant="tertiary" size="sm" onClick={() => bulkAction.mutate({ action: 'reschedule' })} disabled={bulkAction.isPending}>Reschedule</Button>
          <Button
            variant="danger"
            size="sm"
            onClick={() => setConfirmAction({ action: 'bulkDelete', title: 'Delete Jobs', message: `Permanently delete ${selectedJobs.size} selected job(s)? This cannot be undone.`, confirmText: 'Delete All', confirmVariant: 'danger' })}
            disabled={bulkAction.isPending}
          >
            Delete
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setSelectedJobs(new Set())} className="ml-auto">Clear</Button>
        </div>
      )}

      {compact ? (
        <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] overflow-hidden">
          <JobCardList
            jobs={filteredJobs}
            isLoading={isLoading}
            emptyText={jobTypeFilter === 'order' ? 'No order jobs found' : jobTypeFilter === 'adhoc' ? 'No ad-hoc jobs found' : 'No jobs found'}
            selectedIds={selectedJobs}
            onToggleSelect={toggleJobSelect}
            onSelectAll={() => toggleSelectAll(selectableIds)}
            allSelected={selectableIds.length > 0 && selectableIds.every(id => selectedJobs.has(id))}
            onAction={handleAction}
            dispatchFencedIds={dispatchFencedIds}
            dispatchingId={dispatchJob.isPending ? dispatchJob.variables : null}
            correctionPrincipal={recentPrincipal}
            printers={printersData || []}
            extraJob={deepExtra}
            highlightId={deepLinkId}
          />
        </div>
      ) : (
      <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] overflow-hidden">
        <div className="overflow-x-auto" tabIndex={0} role={foundry ? 'region' : undefined} aria-label="Jobs table">
          <Table className="w-full min-w-[600px]">
            <JobTableHeader
              sortField={sortField}
              sortDirection={sortDirection}
              onSort={toggleSort}
              allSelected={selectableIds.length > 0 && selectableIds.every(id => selectedJobs.has(id))}
              onSelectAll={() => toggleSelectAll(selectableIds)}
            />
            <tbody>
              {deepExtra && (
                <JobRow key={`deep-${deepExtra.id}`} job={deepExtra} highlight correctionPrincipal={recentPrincipal} onAction={handleAction} isSelected={selectedJobs.has(deepExtra.id)} onToggleSelect={toggleJobSelect}
                  pinnedPrinterName={deepExtra.pinned_printer_id != null ? (printersData || []).find(p => p.id === deepExtra.pinned_printer_id)?.name : undefined}
                  dispatchFenced={dispatchFencedIds?.has(deepExtra.id)} isDispatching={dispatchJob.isPending && dispatchJob.variables === deepExtra.id} />
              )}
              {isLoading ? (
                <tr><td colSpan={9} className="px-4 py-8 text-center text-[var(--brand-text-muted)] text-sm"><PageState kind="loading" className="flex items-center justify-center gap-2"><RefreshCw size={14} className="animate-spin" />Loading...</PageState></td></tr>
              ) : filteredJobs.length === 0 ? (
                <tr><td colSpan={9} className="px-4 py-8 text-center text-[var(--brand-text-muted)] text-sm">
                  {jobTypeFilter === 'order' ? 'No order jobs found' :
                   jobTypeFilter === 'adhoc' ? 'No ad-hoc jobs found' : 'No jobs found'}
                </td></tr>
              ) : (
                filteredJobs.map(job => (
                  <JobRow key={job.id} job={job} pinnedPrinterName={job.pinned_printer_id != null ? (printersData || []).find(p => p.id === job.pinned_printer_id)?.name : undefined} correctionPrincipal={recentPrincipal} onAction={handleAction} isSelected={selectedJobs.has(job.id)} highlight={job.id === deepLinkId} onToggleSelect={toggleJobSelect} dispatchFenced={dispatchFencedIds?.has(job.id)} isDispatching={dispatchJob.isPending && dispatchJob.variables === job.id}
                    // Drag only in queue order: positions written from another sort would be meaningless.
                    dragProps={canDo('jobs.edit') && sortField === 'queue' && serverOrder === 'queue' && (job.status === 'pending' || job.status === 'scheduled') ? {
                      draggable: true,
                      onDragStart: (e) => handleDragStart(e, job.id),
                      onDragEnd: handleDragEnd,
                      onDragOver: (e) => handleDragOver(e, job.id),
                      onDrop: (e) => handleDrop(e, job.id),
                      style: { borderTop: dragOverId === job.id ? '2px solid #d97706' : undefined, cursor: 'grab' }
                    } : undefined} />
                ))
              )}
            </tbody>
          </Table>
        </div>
      </div>

      )}
      <div className="flex items-center gap-3 mt-3 mb-4 text-sm text-[var(--brand-text-secondary)]">
        {jobsTotal != null && <span>{jobTypeFilter === 'all'
          ? `Showing ${jobsData?.length ?? 0} of ${jobsTotal}`
          : `Showing ${typeFilteredJobs.length} on this tab (${jobsData?.length ?? 0} of ${jobsTotal} jobs loaded)`}</span>}
        {jobsQuery.hasNextPage && (
          <Button variant="secondary" size="sm" onClick={() => { void jobsQuery.fetchNextPage() }} disabled={jobsQuery.isFetchingNextPage} loading={jobsQuery.isFetchingNextPage}>Load more</Button>
        )}
        {deepLinkId != null && deepMissing && (
          <div role="status" className="text-amber-400">{`Job ${deepLinkId} was not found, or you cannot see it.`}</div>
        )}
      </div>

      <RecentlyCompleted jobs={recentJobs} loading={recentLoading} error={recentError} onRetry={() => { void retryRecent() }} />

      {failureModal && (
        <FailureReasonModal
          jobId={failureModal.jobId}
          jobName={failureModal.jobName}
          existingReason={failureModal.existingReason}
          existingNotes={failureModal.existingNotes}
          onSubmit={handleFailureSubmit}
          onClose={() => setFailureModal(null)}
        />
      )}
      <CreateJobModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSubmit={(data) => createJob.mutate(data)}
        onSavePreset={(data) => createPreset.mutate(data)}
        modelsData={modelsData}
        printersData={printersData}
      />
      {correctingJob && <JobCorrectionModal key={correctingJob.job.id} job={correctingJob.job} actor={correctingJob.actor} onClose={closeCorrection} onSubmit={(id, data) => jobs.update(id, data)} />}
      <EditJobModal
        isOpen={!!editingJob}
        onClose={() => setEditingJob(null)}
        onSubmit={(id, data) => updateJob.mutate({ id, data })}
        job={editingJob}
        printersData={printersData}
        modelsData={modelsData}
      />
      <RejectModal
        isOpen={showRejectModal}
        onClose={() => { setShowRejectModal(false); setRejectingJobId(null) }}
        onSubmit={(reason) => rejectJobMut.mutate({ jobId: rejectingJobId, reason })}
      />
      <ConfirmModal
        open={!!confirmAction}
        onConfirm={handleConfirmAction}
        onCancel={() => setConfirmAction(null)}
        title={confirmAction?.title || ''}
        message={confirmAction?.message || ''}
        confirmText={confirmAction?.confirmText || 'Confirm'}
        confirmVariant={confirmAction?.confirmVariant || 'danger'}
      >
        {confirmAction?.action === 'dispatch' && jobsData?.find(j => j.id === confirmAction.jobId)?.printer_id && <MaintenanceBanner printerId={jobsData.find(j => j.id === confirmAction.jobId).printer_id} />}
      </ConfirmModal>
    </div>
  )
}
