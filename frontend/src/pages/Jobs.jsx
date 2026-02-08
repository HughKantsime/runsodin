import { resolveColor } from "../utils/colorMap"

function formatHours(h) {
  if (!h) return "—"
  if (h < 1) return Math.round(h * 60) + "m"
  const hrs = Math.floor(h)
  const mins = Math.round((h - hrs) * 60)
  return mins > 0 ? hrs + "h " + mins + "m" : hrs + "h"
}
import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Play, CheckCircle, XCircle, RotateCcw, Trash2, Filter, Search, ArrowUp, ArrowDown, ArrowUpDown, ShoppingCart, Layers, Zap, RefreshCw, Clock, MessageSquare, AlertTriangle , Calendar, Flag, History } from 'lucide-react'
import { format } from 'date-fns'
import clsx from 'clsx'
import { jobs, models, printers, scheduler, approveJob, rejectJob, resubmitJob, getApprovalSetting } from '../api'
import { canDo } from '../permissions'
import FailureReasonModal from '../components/FailureReasonModal'
import { updateJobFailure } from '../api'

const statusOptions = [
  { value: '', label: 'All Status' },
  { value: 'submitted', label: 'Submitted' },
  { value: 'pending', label: 'Pending' },
  { value: 'scheduled', label: 'Scheduled' },
  { value: 'printing', label: 'Printing' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
  { value: 'rejected', label: 'Rejected' },
]

const priorityOptions = [
  { value: 1, label: '1 - Highest' },
  { value: 2, label: '2 - High' },
  { value: 3, label: '3 - Normal' },
  { value: 4, label: '4 - Low' },
  { value: 5, label: '5 - Lowest' },
]

const statusOrder = { submitted: 0, printing: 1, scheduled: 2, pending: 3, rejected: 4, failed: 5, completed: 6 }

const jobTypeTabs = [
  { value: 'all', label: 'All Jobs', icon: Layers },
  { value: 'approval', label: 'Awaiting Approval', icon: Clock },
  { value: 'order', label: 'Order Jobs', icon: ShoppingCart },
  { value: 'adhoc', label: 'Ad-hoc', icon: null },
]

function JobRow({ job, onAction, dragProps }) {
  const statusColors = {
    submitted: 'text-amber-400',
    pending: 'text-status-pending',
    scheduled: 'text-status-scheduled',
    printing: 'text-status-printing',
    completed: 'text-status-completed',
    failed: 'text-status-failed',
    rejected: 'text-red-400',
  }



  return (
    <tr className="border-b border-farm-800 hover:bg-farm-900/50"
        {...(dragProps || {})}>
      <td className="px-3 md:px-4 py-3">
        <div className="flex items-center gap-2">
          <div className={clsx('status-dot', job.status)} />
          <span className={clsx('text-sm', statusColors[job.status])}>{job.status}</span>
        </div>
      </td>
      <td className="px-3 md:px-4 py-3">
        <div className="font-medium text-sm">{job.item_name}</div>
        {job.order_item_id && (
          <div className="text-xs text-print-400 flex items-center gap-1">
            <ShoppingCart size={10} />
            Order #{job.order_item?.order_id || '—'}
          </div>
        )}
        {job.rejected_reason && (
          <div className="text-xs text-red-400 flex items-center gap-1 truncate max-w-xs">
            <MessageSquare size={10} />
            {job.rejected_reason}
          </div>
        )}
        {job.notes && <div className="text-xs text-farm-500 truncate max-w-xs">{job.notes}</div>}
        {job.due_date && <DueDateBadge dueDate={job.due_date} />}
        {job.fail_reason && (
          <div className="text-xs text-red-400 truncate max-w-xs">
            ⚠ {job.fail_reason.replace(/_/g, ' ')}{job.fail_notes ? `: ${job.fail_notes}` : ''}
          </div>
        )}
      </td>
      <td className="px-3 md:px-4 py-3">
        <span className={clsx(
          'px-2 py-0.5 rounded text-xs font-medium',
          job.priority <= 2 ? 'bg-red-900/50 text-red-400' : 
          job.priority >= 4 ? 'bg-farm-800 text-farm-400' : 
          'bg-amber-900/50 text-amber-400'
        )}>
          P{job.priority}
        </span>
      </td>
      <td className="px-3 md:px-4 py-3 text-sm">{job.printer?.name || '—'}</td>
      <td className="px-3 md:px-4 py-3 hidden lg:table-cell">
        {job.colors_list?.length > 0 ? (
          <div className="flex gap-1 flex-wrap">
            {job.colors_list.map((color, i) => (
              <span key={i} className="w-5 h-5 rounded-full border border-farm-700 flex items-center justify-center" style={{ backgroundColor: resolveColor(color) || "#333" }} title={color}>{!resolveColor(color) && <span className="text-[10px] text-farm-400">?</span>}</span>
            ))}
          </div>
        ) : '—'}
      </td>
      <td className="px-3 md:px-4 py-3 text-sm text-farm-400 hidden md:table-cell">{formatHours(job.duration_hours)}</td>
      <td className="px-3 md:px-4 py-3 text-sm text-farm-400 hidden lg:table-cell">
        {job.scheduled_start ? format(new Date(job.scheduled_start), 'MMM d HH:mm') : '—'}
      </td>
      <td className="px-3 md:px-4 py-3">
        <div className="flex items-center gap-1">
          {job.status === 'submitted' && (
            <>
              <button onClick={() => onAction('approve', job.id)} className="p-1.5 text-green-400 hover:bg-green-900/50 rounded" title="Approve">
                <CheckCircle size={16} />
              </button>
              <button onClick={() => onAction('reject', job.id)} className="p-1.5 text-red-400 hover:bg-red-900/50 rounded" title="Reject">
                <XCircle size={16} />
              </button>
            </>
          )}
          {job.status === 'rejected' && job.submitted_by && (
            <button onClick={() => onAction('resubmit', job.id)} className="p-1.5 text-amber-400 hover:bg-amber-900/50 rounded" title="Resubmit">
              <RefreshCw size={14} />
            </button>
          )}
          {job.status === 'scheduled' && (
            <button onClick={() => onAction('start', job.id)} className="p-1.5 text-print-400 hover:bg-print-900/50 rounded" title="Start Print">
              <Play size={16} />
            </button>
          )}
          {job.status === 'printing' && (
            <>
              <button onClick={() => onAction('complete', job.id)} className="p-1.5 text-green-400 hover:bg-green-900/50 rounded" title="Complete">
                <CheckCircle size={16} />
              </button>
              <button onClick={() => onAction('markFailed', job.id, job.item_name)} className="p-1.5 text-red-400 hover:bg-red-900/50 rounded" title="Mark Failed">
                <AlertTriangle size={16} />
              </button>
            </>
          )}
          {canDo('jobs.cancel') && (job.status === 'scheduled' || job.status === 'printing') && (
            <button onClick={() => onAction('cancel', job.id)} className="p-1.5 text-red-400 hover:bg-red-900/50 rounded" title="Cancel">
              <XCircle size={16} />
            </button>
          )}
          {job.status === 'failed' && (
            <button onClick={() => onAction('failReason', job.id, job.item_name, job.fail_reason, job.fail_notes)} className="p-1.5 text-amber-400 hover:bg-amber-900/50 rounded" title={job.fail_reason ? 'Edit Failure Reason' : 'Add Failure Reason'}>
              <AlertTriangle size={16} />
            </button>
          )}
          {canDo('jobs.delete') && (job.status === 'pending' || job.status === 'scheduled' || job.status === 'failed') && (
            <>
              <button onClick={() => onAction('repeat', job.id)} className="p-1.5 text-farm-400 hover:text-print-400 hover:bg-print-900/50 rounded" title="Print Again">
                <RefreshCw size={14} />
              </button>
              <button onClick={() => onAction('delete', job.id)} className="p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded" title="Delete">
                <Trash2 size={16} />
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  )
}

function SortIcon({ field, sortField, sortDirection }) {
  if (sortField !== field) return <ArrowUpDown size={12} className="opacity-30" />
  return sortDirection === 'asc' ? <ArrowUp size={12} /> : <ArrowDown size={12} />
}

function CreateJobModal({ isOpen, onClose, onSubmit, modelsData }) {
  const [formData, setFormData] = useState({
    item_name: '',
    model_id: '',
    priority: 3,
    duration_hours: '',
    colors_required: '',
    notes: '',
    due_date: '',
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    onSubmit({
      ...formData,
      model_id: formData.model_id ? Number(formData.model_id) : null,
      duration_hours: formData.duration_hours ? Number(formData.duration_hours) : null,
      due_date: formData.due_date || null,
    })
    setFormData({ item_name: '', model_id: '', priority: 3, duration_hours: '', colors_required: '', notes: '', due_date: '' })
    onClose()
  }

  const handleModelSelect = (modelId) => {
    const model = modelsData?.find(m => m.id === Number(modelId))
    setFormData(prev => ({
      ...prev,
      model_id: modelId,
      item_name: model?.name || prev.item_name,
      duration_hours: model?.build_time_hours || prev.duration_hours,
      colors_required: model?.required_colors?.join(', ') || prev.colors_required,
    }))
  }

  if (!isOpen) return null

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4">
      <div className="bg-farm-900 rounded-t-xl sm:rounded w-full max-w-lg p-4 sm:p-6 border border-farm-700 max-h-[90vh] overflow-y-auto">
        <h2 className="text-lg sm:text-xl font-display font-semibold mb-4">Create New Job</h2>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Model (optional)</label>
            <select value={formData.model_id} onChange={(e) => handleModelSelect(e.target.value)} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
              <option value="">Select a model...</option>
              {modelsData?.map(model => (
                <option key={model.id} value={model.id}>{model.name}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Item Name *</label>
            <input type="text" required value={formData.item_name} onChange={(e) => setFormData(prev => ({ ...prev, item_name: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-farm-400 mb-1">Priority</label>
              <select value={formData.priority} onChange={(e) => setFormData(prev => ({ ...prev, priority: Number(e.target.value) }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
                {priorityOptions.map(opt => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm text-farm-400 mb-1">Duration (hours)</label>
              <input type="number" step="0.5" value={formData.duration_hours} onChange={(e) => setFormData(prev => ({ ...prev, duration_hours: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
            </div>
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Colors Required</label>
            <input type="text" value={formData.colors_required} onChange={(e) => setFormData(prev => ({ ...prev, colors_required: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="e.g., black, white, green matte" />
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Due Date (optional)</label>
            <input type="date" value={formData.due_date} onChange={(e) => setFormData(prev => ({ ...prev, due_date: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Notes</label>
            <textarea value={formData.notes} onChange={(e) => setFormData(prev => ({ ...prev, notes: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" rows={2} />
          </div>
          <div className="flex justify-end gap-3 pt-4">
            <button type="button" onClick={onClose} className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm">Cancel</button>
            <button type="submit" className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-sm">Create Job</button>
          </div>
        </form>
      </div>
    </div>
  )
}


function RejectModal({ isOpen, onClose, onSubmit }) {
  const [reason, setReason] = useState('')
  if (!isOpen) return null
  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4">
      <div className="bg-farm-900 rounded-t-xl sm:rounded w-full max-w-md p-4 sm:p-6 border border-farm-700">
        <h2 className="text-lg font-display font-semibold mb-4">Reject Job</h2>
        <div className="mb-4">
          <label className="block text-sm text-farm-400 mb-1">Reason (required)</label>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="e.g., Too much filament. Please re-slice with 10% infill."
            className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm"
            rows={3}
            autoFocus
          />
        </div>
        <div className="flex justify-end gap-3">
          <button onClick={() => { onClose(); setReason('') }} className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm">Cancel</button>
          <button
            onClick={() => { if (reason.trim()) { onSubmit(reason.trim()); setReason(''); onClose() } }}
            disabled={!reason.trim()}
            className="px-4 py-2 bg-red-600 hover:bg-red-500 disabled:opacity-50 rounded-lg text-sm text-white"
          >
            Reject
          </button>
        </div>
      </div>
    </div>
  )
}



function RecentlyCompleted({ jobs }) {
  const recent = jobs
    ?.filter(j => j.status === 'completed' || j.status === 'failed')
    .sort((a, b) => new Date(b.updated_at || b.created_at) - new Date(a.updated_at || a.created_at))
    .slice(0, 8)
  
  if (!recent || recent.length === 0) return null
  
  return (
    <div className="mt-6">
      <h3 className="text-sm font-display font-semibold text-farm-400 mb-3 flex items-center gap-2">
        <History size={14} />
        Recently Completed
      </h3>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-2">
        {recent.map(job => (
          <div key={job.id} className={`bg-farm-900 rounded-lg border p-3 ${
            job.status === 'failed' ? 'border-red-900/50' : 'border-farm-800'
          }`}>
            <div className="flex items-center justify-between mb-1">
              <span className="text-sm font-medium truncate">{job.item_name}</span>
              {job.status === 'completed' 
                ? <CheckCircle size={14} className="text-green-400 flex-shrink-0" />
                : <XCircle size={14} className="text-red-400 flex-shrink-0" />
              }
            </div>
            <div className="text-xs text-farm-500">
              {job.printer?.name || 'Unknown printer'}
              {job.duration_hours ? ` · ${job.duration_hours}h` : ''}
            </div>
            {job.fail_reason && (
              <div className="text-xs text-red-400 mt-1 truncate">⚠ {job.fail_reason.replace(/_/g, ' ')}</div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

const PRIORITY_CONFIG = {
  urgent: { color: 'bg-red-900/50 text-red-400 border-red-800', label: 'Urgent', icon: '🔴' },
  high: { color: 'bg-orange-900/50 text-orange-400 border-orange-800', label: 'High', icon: '🟠' },
  normal: { color: 'bg-farm-800 text-farm-400 border-farm-700', label: 'Normal', icon: '' },
  low: { color: 'bg-blue-900/50 text-blue-400 border-blue-800', label: 'Low', icon: '🔵' },
}

function PriorityBadge({ priority }) {
  if (!priority || priority === 'normal') return null
  const cfg = PRIORITY_CONFIG[priority] || PRIORITY_CONFIG.normal
  return (
    <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs border ${cfg.color}`}>
      {cfg.icon && <span className="text-[10px]">{cfg.icon}</span>}
      {cfg.label}
    </span>
  )
}

function DueDateBadge({ dueDate }) {
  if (!dueDate) return null
  const due = new Date(dueDate)
  const now = new Date()
  const daysUntil = Math.ceil((due - now) / (1000 * 60 * 60 * 24))
  
  let color = 'text-farm-400'
  if (daysUntil < 0) color = 'text-red-400'
  else if (daysUntil <= 1) color = 'text-orange-400'
  else if (daysUntil <= 3) color = 'text-yellow-400'
  
  const label = daysUntil < 0 
    ? `${Math.abs(daysUntil)}d overdue`
    : daysUntil === 0 ? 'Due today'
    : daysUntil === 1 ? 'Due tomorrow'
    : `Due in ${daysUntil}d`
    
  return (
    <span className={`inline-flex items-center gap-1 text-xs ${color}`}>
      <Calendar size={10} />
      {label}
    </span>
  )
}

export default function Jobs() {
  const queryClient = useQueryClient()
  const runScheduler = useMutation({
    mutationFn: scheduler.run,
    onSuccess: () => {
      queryClient.invalidateQueries(['jobs'])
      queryClient.invalidateQueries(['stats'])
    }
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
  // Drag-and-drop queue reorder
  const [draggedId, setDraggedId] = useState(null)
  const [dragOverId, setDragOverId] = useState(null)
  const handleDragStart = (e, jobId) => {
    setDraggedId(jobId)
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', jobId)
    e.currentTarget.style.opacity = '0.4'
  }
  const handleDragEnd = (e) => {
    e.currentTarget.style.opacity = '1'
    setDraggedId(null)
    setDragOverId(null)
  }
  const handleDragOver = (e, jobId) => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'move'
    if (jobId !== draggedId) setDragOverId(jobId)
  }
  const handleDrop = async (e, targetId) => {
    e.preventDefault()
    setDragOverId(null)
    setDraggedId(null)
    if (!draggedId || draggedId === targetId) return
    const currentJobs = (jobsData || []).filter(j => j.status === 'pending' || j.status === 'scheduled')
    const fromIdx = currentJobs.findIndex(j => j.id === draggedId)
    const toIdx = currentJobs.findIndex(j => j.id === targetId)
    if (fromIdx === -1 || toIdx === -1) return
    const reordered = [...currentJobs]
    const [moved] = reordered.splice(fromIdx, 1)
    reordered.splice(toIdx, 0, moved)
    try {
      await jobs.reorder(reordered.map(j => j.id))
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
    } catch (err) {
      console.error('Reorder failed:', err)
    }
  }

  const { data: approvalSetting } = useQuery({
    queryKey: ['approval-setting'],
    queryFn: getApprovalSetting,
  })

  const { data: jobsData, isLoading } = useQuery({
    queryKey: ['jobs', statusFilter],
    queryFn: () => jobs.list(statusFilter || null),
  })

  const { data: modelsData } = useQuery({
    queryKey: ['models'],
    queryFn: () => models.list(),
  })

  const createJob = useMutation({
    mutationFn: jobs.create,
    onSuccess: () => queryClient.invalidateQueries(['jobs']),
  })

  const startJob = useMutation({
    mutationFn: jobs.start,
    onSuccess: () => queryClient.invalidateQueries(['jobs']),
  })

  const completeJob = useMutation({
    mutationFn: jobs.complete,
    onSuccess: () => queryClient.invalidateQueries(['jobs']),
  })

  const cancelJob = useMutation({
    mutationFn: jobs.cancel,
    onSuccess: () => queryClient.invalidateQueries(['jobs']),
  })

  


  const deleteJob = useMutation({
    mutationFn: jobs.delete,
    onSuccess: () => queryClient.invalidateQueries(['jobs']),
  })

  const repeatJob = useMutation({
    mutationFn: async (jobId) => {
      const token = localStorage.getItem('token')
      const headers = { 'Content-Type': 'application/json', 'X-API-Key': API_KEY }
      if (token) headers['Authorization'] = 'Bearer ' + token
      const res = await fetch(API_BASE + '/jobs/' + jobId + '/repeat', { method: 'POST', headers })
      if (!res.ok) throw new Error('Failed to repeat job')
      return res.json()
    },
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] })
  })

  const approveJobMut = useMutation({
    mutationFn: approveJob,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
  })

  const rejectJobMut = useMutation({
    mutationFn: ({ jobId, reason }) => rejectJob(jobId, reason),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
  })

  const resubmitJobMut = useMutation({
    mutationFn: resubmitJob,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
  })

  const handleAction = (action, jobId, jobName, existingReason, existingNotes) => {
    switch (action) {
      case 'start': startJob.mutate(jobId); break
      case 'complete': completeJob.mutate(jobId); break
      case 'cancel': cancelJob.mutate(jobId); break
      case 'repeat': repeatJob.mutate(jobId); break
      case 'delete': if (confirm('Delete this job?')) deleteJob.mutate(jobId); break
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
    <div className="p-4 md:p-8">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4 md:mb-6">
        <div>
          <h1 className="text-2xl md:text-3xl font-display font-bold">Jobs</h1>
          <p className="text-farm-500 text-sm mt-1">Manage print queue</p>
        </div>
        <div className="flex items-center gap-2 self-start">
          <button onClick={() => runScheduler.mutate()} disabled={runScheduler.isPending}
            className={clsx('flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors bg-farm-800 hover:bg-farm-700 border border-farm-700', runScheduler.isPending && 'opacity-50 cursor-not-allowed')}>
            <Zap size={16} />
            {runScheduler.isPending ? 'Running...' : 'Run Scheduler'}
          </button>
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
               tab.value === 'order' ? orderJobCount : adhocJobCount}
            </span>
          </button>
        ))}
      </div>

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

      <div className="bg-farm-900 rounded border border-farm-800 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[600px]">
            <thead className="bg-farm-950 border-b border-farm-800">
              <tr>
                <th className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400 cursor-pointer hover:text-farm-200 select-none" onClick={() => toggleSort('status')}>
                  <div className="flex items-center gap-1">Status <SortIcon field="status" sortField={sortField} sortDirection={sortDirection} /></div>
                </th>
                <th className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400 cursor-pointer hover:text-farm-200 select-none" onClick={() => toggleSort('item_name')}>
                  <div className="flex items-center gap-1">Item <SortIcon field="item_name" sortField={sortField} sortDirection={sortDirection} /></div>
                </th>
                <th className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400 cursor-pointer hover:text-farm-200 select-none" onClick={() => toggleSort('priority')}>
                  <div className="flex items-center gap-1">Pri <SortIcon field="priority" sortField={sortField} sortDirection={sortDirection} /></div>
                </th>
                <th className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400 cursor-pointer hover:text-farm-200 select-none" onClick={() => toggleSort('printer')}>
                  <div className="flex items-center gap-1">Printer <SortIcon field="printer" sortField={sortField} sortDirection={sortDirection} /></div>
                </th>
                <th className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400 hidden lg:table-cell">Colors</th>
                <th className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400 hidden md:table-cell cursor-pointer hover:text-farm-200 select-none" onClick={() => toggleSort('duration_hours')}>
                  <div className="flex items-center gap-1">Duration <SortIcon field="duration_hours" sortField={sortField} sortDirection={sortDirection} /></div>
                </th>
                <th className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400 hidden lg:table-cell cursor-pointer hover:text-farm-200 select-none" onClick={() => toggleSort('scheduled_start')}>
                  <div className="flex items-center gap-1">Scheduled <SortIcon field="scheduled_start" sortField={sortField} sortDirection={sortDirection} /></div>
                </th>
                <th className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400">Actions</th>
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                <tr><td colSpan={8} className="px-4 py-8 text-center text-farm-500 text-sm">Loading...</td></tr>
              ) : filteredJobs.length === 0 ? (
                <tr><td colSpan={8} className="px-4 py-8 text-center text-farm-500 text-sm">
                  {jobTypeFilter === 'order' ? 'No order jobs found' :
                   jobTypeFilter === 'adhoc' ? 'No ad-hoc jobs found' : 'No jobs found'}
                </td></tr>
              ) : (
                filteredJobs.map(job => (
                  <JobRow key={job.id} job={job} onAction={handleAction}
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
        </div>
      </div>
      <CreateJobModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        onSubmit={(data) => createJob.mutate(data)}
        modelsData={modelsData}
      />
    </div>
  )
}
