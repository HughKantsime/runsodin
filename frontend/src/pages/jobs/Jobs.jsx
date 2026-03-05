import { useState, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Filter, ShoppingCart, Layers, Zap, RefreshCw, Clock, Briefcase } from 'lucide-react'
import { jobs, models, printers as printersApi, scheduler, getApprovalSetting, presets, bulkOps } from '../../api'
import { canDo } from '../../permissions'
import { useOrg } from '../../contexts/OrgContext'
import FailureReasonModal from '../../components/jobs/FailureReasonModal'
import { updateJobFailure } from '../../api'
import toast from 'react-hot-toast'
import ConfirmModal from '../../components/shared/ConfirmModal'
import JobRow from '../../components/jobs/JobRow'
import RecentlyCompleted from '../../components/jobs/RecentlyCompleted'
import { CreateJobModal, EditJobModal, RejectModal } from '../../components/jobs/JobModals'
import { statusOptions, statusOrder } from '../../components/jobs/jobUtils'
import { useJobMutations } from '../../hooks/useJobMutations'
import JobTableHeader from '../../components/jobs/JobTableHeader'
import { PageHeader, Button, SearchInput, TabBar } from '../../components/ui'


const jobTypeTabs = [
  { value: 'all', label: 'All Jobs', icon: Layers },
  { value: 'approval', label: 'Awaiting Approval', icon: Clock },
  { value: 'order', label: 'Order Jobs', icon: ShoppingCart },
  { value: 'adhoc', label: 'Ad-hoc', icon: null },
]

export default function Jobs() {
  const org = useOrg()
  const queryClient = useQueryClient()
  const {
    createJob, updateJob, startJob, completeJob, cancelJob,
    deleteJob, repeatJob, dispatchJob, approveJobMut, rejectJobMut, resubmitJobMut,
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
  const [sortField, setSortField] = useState('priority')
  const [sortDirection, setSortDirection] = useState('asc')
  const [jobTypeFilter, _setJobTypeFilter] = useState(() => searchParams.get('type') || 'all')

  const updateSearchParams = useCallback((updates) => {
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
  const [showRejectModal, setShowRejectModal] = useState(false)
  const [rejectingJobId, setRejectingJobId] = useState(null)
  const [failureModal, setFailureModal] = useState(null)
  const [editingJob, setEditingJob] = useState(null)
  const [selectedJobs, setSelectedJobs] = useState(new Set())
  const [confirmAction, setConfirmAction] = useState(null)
  const [draggedId, setDraggedId] = useState(null)
  const [dragOverId, setDragOverId] = useState(null)

  const toggleJobSelect = (id) => setSelectedJobs(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n })
  const toggleSelectAll = (jobIds) => setSelectedJobs(prev => prev.size === jobIds.length ? new Set() : new Set(jobIds))

  const bulkAction = useMutation({
    mutationFn: ({ action, extra }) => bulkOps.jobs([...selectedJobs], action, extra),
    onSuccess: (_, vars) => { queryClient.invalidateQueries({ queryKey: ['jobs'] }); setSelectedJobs(new Set()); toast.success(`Bulk ${vars.action} completed`) },
    onError: (err, vars) => toast.error(`Bulk ${vars.action} failed: ${err.message}`),
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
  const { data: jobsData, isLoading } = useQuery({ queryKey: ['jobs', statusFilter, org.orgId], queryFn: () => jobs.list(statusFilter || null, org.orgId) })
  const { data: modelsData } = useQuery({ queryKey: ['models', org.orgId], queryFn: () => models.list(org.orgId) })
  const { data: printersData } = useQuery({ queryKey: ['printers'], queryFn: () => printersApi.list() })
  const { data: presetsData } = useQuery({ queryKey: ['presets'], queryFn: () => presets.list() })

  const handleDragStart = (e, jobId) => { setDraggedId(jobId); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', jobId); e.currentTarget.style.opacity = '0.4' }
  const handleDragEnd = (e) => { e.currentTarget.style.opacity = '1'; setDraggedId(null); setDragOverId(null) }
  const handleDragOver = (e, jobId) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; if (jobId !== draggedId) setDragOverId(jobId) }
  const handleDrop = async (e, targetId) => {
    e.preventDefault(); setDragOverId(null); setDraggedId(null)
    if (!draggedId || draggedId === targetId) return
    const currentJobs = (jobsData || []).filter(j => j.status === 'pending' || j.status === 'scheduled')
    const fromIdx = currentJobs.findIndex(j => j.id === draggedId)
    const toIdx = currentJobs.findIndex(j => j.id === targetId)
    if (fromIdx === -1 || toIdx === -1) return
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
        const job = (jobsData || []).find(j => j.id === jobId)
        if (job) setEditingJob(job)
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
        cancelJob.mutate(jobId, {
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
  const typeFilteredJobs = (jobsData || []).filter(job => {
    if (jobTypeFilter === 'approval') return job.status === 'submitted'
    if (jobTypeFilter === 'order') return job.order_item_id != null
    if (jobTypeFilter === 'adhoc') return job.order_item_id == null
    return true
  })

  // Then filter by search and sort
  const filteredJobs = typeFilteredJobs
    .filter(job => !searchQuery || job.item_name.toLowerCase().includes(searchQuery.toLowerCase()))
    .sort((a, b) => {
      const dir = sortDirection === 'asc' ? 1 : -1
      switch (sortField) {
        case 'status': return dir * ((statusOrder[a.status] ?? 99) - (statusOrder[b.status] ?? 99))
        case 'item_name': return dir * (a.item_name || '').localeCompare(b.item_name || '')
        case 'priority': return dir * ((a.priority || 99) - (b.priority || 99))
        case 'printer': return dir * ((a.printer?.name || '').localeCompare(b.printer?.name || ''))
        case 'duration_hours': return dir * ((a.duration_hours || 0) - (b.duration_hours || 0))
        case 'scheduled_start': return dir * (new Date(a.scheduled_start || 0) - new Date(b.scheduled_start || 0))
        default: return 0
      }
    })

  // Count jobs by type for badge display
  const approvalJobCount = (jobsData || []).filter(j => j.status === 'submitted').length
  const orderJobCount = (jobsData || []).filter(j => j.order_item_id != null).length
  const adhocJobCount = (jobsData || []).filter(j => j.order_item_id == null).length

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

      {/* Job Type Tabs */}
      <div className="mb-4">
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
      </div>

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
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          placeholder="Search jobs..."
          className="flex-1 sm:max-w-md"
        />
        <div className="flex items-center gap-2">
          <Filter size={16} className="text-[var(--brand-text-muted)]" />
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="bg-[var(--brand-card-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm">
            {statusOptions.map(opt => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>
      </div>

      {selectedJobs.size > 0 && (
        <div className="flex items-center gap-3 mb-3 p-3 bg-[var(--brand-primary)]/10 border border-[var(--brand-primary)]/30 rounded-md">
          <span className="text-sm text-[var(--brand-text-secondary)]">{selectedJobs.size} selected</span>
          <Button variant="tertiary" size="sm" onClick={() => bulkAction.mutate({ action: 'cancel' })} disabled={bulkAction.isPending}>Cancel</Button>
          <Button variant="tertiary" size="sm" onClick={() => bulkAction.mutate({ action: 'reprioritize', extra: { priority: 1 } })} disabled={bulkAction.isPending}>Priority 1</Button>
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

      <div className="bg-[var(--brand-card-bg)] rounded-md border border-[var(--brand-card-border)] overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[600px]">
            <JobTableHeader
              sortField={sortField}
              sortDirection={sortDirection}
              onSort={toggleSort}
              allSelected={filteredJobs.length > 0 && selectedJobs.size === filteredJobs.length}
              onSelectAll={() => toggleSelectAll(filteredJobs.map(j => j.id))}
            />
            <tbody>
              {isLoading ? (
                <tr><td colSpan={9} className="px-4 py-8 text-center text-[var(--brand-text-muted)] text-sm"><div className="flex items-center justify-center gap-2"><RefreshCw size={14} className="animate-spin" />Loading...</div></td></tr>
              ) : filteredJobs.length === 0 ? (
                <tr><td colSpan={9} className="px-4 py-8 text-center text-[var(--brand-text-muted)] text-sm">
                  {jobTypeFilter === 'order' ? 'No order jobs found' :
                   jobTypeFilter === 'adhoc' ? 'No ad-hoc jobs found' : 'No jobs found'}
                </td></tr>
              ) : (
                filteredJobs.map(job => (
                  <JobRow key={job.id} job={job} onAction={handleAction} isSelected={selectedJobs.has(job.id)} onToggleSelect={toggleJobSelect} isDispatching={dispatchJob.isPending && dispatchJob.variables === job.id}
                    dragProps={(job.status === 'pending' || job.status === 'scheduled') ? {
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
          </table>
        </div>
      </div>

      <RecentlyCompleted jobs={jobsData} />

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
      />
    </div>
  )
}
