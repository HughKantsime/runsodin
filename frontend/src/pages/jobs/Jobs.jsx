import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Filter, Search, ShoppingCart, Layers, Zap, RefreshCw, Clock, Briefcase } from 'lucide-react'
import clsx from 'clsx'
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

  const [showCreateModal, setShowCreateModal] = useState(false)
  const [statusFilter, setStatusFilter] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [sortField, setSortField] = useState('priority')
  const [sortDirection, setSortDirection] = useState('asc')
  const [jobTypeFilter, setJobTypeFilter] = useState('all')
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
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4 md:mb-6">
        <div className="flex items-center gap-3">
          <Briefcase className="text-print-400" size={24} />
          <div>
            <h1 className="text-xl md:text-2xl font-display font-bold">Jobs</h1>
            <p className="text-farm-500 text-sm mt-1">Manage print queue</p>
          </div>
        </div>
        <div className="flex items-center gap-2 self-start">
          {canDo('jobs.create') && <button onClick={() => runScheduler.mutate()} disabled={runScheduler.isPending}
            aria-busy={runScheduler.isPending}
            className={clsx('flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors bg-farm-800 hover:bg-farm-700 border border-farm-700', runScheduler.isPending && 'opacity-50 cursor-not-allowed')}>
            <Zap size={16} />
            {runScheduler.isPending ? 'Running...' : 'Run Scheduler'}
          </button>}
          {canDo('jobs.create') && <button onClick={() => setShowCreateModal(true)} className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-sm">
            <Plus size={16} /> New Job
          </button>}
        </div>
      </div>

      {/* Job Type Tabs */}
      <div className="flex gap-1 mb-4 bg-farm-900 p-1 rounded-lg w-fit border border-farm-800">
        {jobTypeTabs.map(tab => (
          <button
            key={tab.value}
            onClick={() => setJobTypeFilter(tab.value)}
            className={clsx(
              'flex items-center gap-2 px-3 py-1.5 rounded-md text-sm font-medium transition-colors',
              jobTypeFilter === tab.value
                ? 'bg-print-600 text-white'
                : 'text-farm-400 hover:text-farm-200 hover:bg-farm-800'
            )}
          >
            {tab.icon && <tab.icon size={14} />}
            <span>{tab.label}</span>
            <span className={clsx(
              'text-xs px-1.5 py-0.5 rounded-full',
              jobTypeFilter === tab.value ? 'bg-print-500' : 'bg-farm-800'
            )}>
              {tab.value === 'all' ? (jobsData?.length || 0) :
               tab.value === 'approval' ? approvalJobCount :
               tab.value === 'order' ? orderJobCount : adhocJobCount}
            </span>
          </button>
        ))}
      </div>

      {/* Quick Schedule from Presets */}
      {presetsData?.length > 0 && (
        <div className="mb-4 bg-farm-900 border border-farm-800 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-2">
            <Zap size={14} className="text-amber-400" />
            <span className="text-xs font-medium text-farm-400 uppercase">Quick Schedule</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {presetsData.map(p => (
              <div key={p.id} className="flex items-center gap-1.5 bg-farm-800 rounded-lg px-3 py-1.5 border border-farm-700">
                <button
                  onClick={() => schedulePreset.mutate(p.id)}
                  className="text-sm text-farm-200 hover:text-white transition-colors"
                  title={`Schedule: ${p.item_name || p.name}`}
                >
                  {p.name}
                </button>
                {canDo('jobs.delete') && (
                  <button onClick={() => deletePreset.mutate(p.id)} className="text-farm-600 hover:text-red-400 ml-1" title="Delete preset">&times;</button>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3 mb-4 md:mb-6">
        <div className="relative flex-1 sm:max-w-md">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500" />
          <input
            type="text"
            placeholder="Search jobs..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-9 pr-4 py-2 bg-farm-900 border border-farm-800 rounded-lg text-sm"
          />
        </div>
        <div className="flex items-center gap-2">
          <Filter size={16} className="text-farm-500" />
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="bg-farm-900 border border-farm-800 rounded-lg px-3 py-2 text-sm">
            {statusOptions.map(opt => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>
      </div>

      {selectedJobs.size > 0 && (
        <div className="flex items-center gap-3 mb-3 p-3 bg-print-900/30 border border-print-700 rounded-lg">
          <span className="text-sm text-farm-300">{selectedJobs.size} selected</span>
          <button onClick={() => bulkAction.mutate({ action: 'cancel' })} disabled={bulkAction.isPending} className="px-3 py-1.5 bg-farm-700 hover:bg-farm-600 rounded-lg text-xs">Cancel</button>
          <button onClick={() => bulkAction.mutate({ action: 'reprioritize', extra: { priority: 1 } })} disabled={bulkAction.isPending} className="px-3 py-1.5 bg-farm-700 hover:bg-farm-600 rounded-lg text-xs">Priority 1</button>
          <button onClick={() => bulkAction.mutate({ action: 'reschedule' })} disabled={bulkAction.isPending} className="px-3 py-1.5 bg-farm-700 hover:bg-farm-600 rounded-lg text-xs">Reschedule</button>
          <button
            onClick={() => setConfirmAction({ action: 'bulkDelete', title: 'Delete Jobs', message: `Permanently delete ${selectedJobs.size} selected job(s)? This cannot be undone.`, confirmText: 'Delete All', confirmVariant: 'danger' })}
            disabled={bulkAction.isPending}
            className="px-3 py-1.5 bg-red-900/50 hover:bg-red-800/50 text-red-300 rounded-lg text-xs"
          >
            Delete
          </button>
          <button onClick={() => setSelectedJobs(new Set())} className="ml-auto px-3 py-1.5 text-farm-500 hover:text-farm-300 text-xs">Clear</button>
        </div>
      )}

      <div className="bg-farm-900 rounded-lg border border-farm-800 overflow-hidden">
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
                <tr><td colSpan={9} className="px-4 py-8 text-center text-farm-500 text-sm"><div className="flex items-center justify-center gap-2"><RefreshCw size={14} className="animate-spin" />Loading...</div></td></tr>
              ) : filteredJobs.length === 0 ? (
                <tr><td colSpan={9} className="px-4 py-8 text-center text-farm-500 text-sm">
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
