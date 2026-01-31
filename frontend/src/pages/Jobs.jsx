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
import { Plus, Play, CheckCircle, XCircle, RotateCcw, Trash2, Filter, Search } from 'lucide-react'
import { format } from 'date-fns'
import clsx from 'clsx'
import { jobs, models, printers } from '../api'

const statusOptions = [
  { value: '', label: 'All Status' },
  { value: 'pending', label: 'Pending' },
  { value: 'scheduled', label: 'Scheduled' },
  { value: 'printing', label: 'Printing' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
]

const priorityOptions = [
  { value: 1, label: '1 - Highest' },
  { value: 2, label: '2 - High' },
  { value: 3, label: '3 - Normal' },
  { value: 4, label: '4 - Low' },
  { value: 5, label: '5 - Lowest' },
]

function JobRow({ job, onAction }) {
  const statusColors = {
    pending: 'text-status-pending',
    scheduled: 'text-status-scheduled',
    printing: 'text-status-printing',
    completed: 'text-status-completed',
    failed: 'text-status-failed',
  }
  return (
    <tr className="border-b border-farm-800 hover:bg-farm-900/50">
      <td className="px-4 py-3">
        <div className="flex items-center gap-2">
          <div className={clsx('status-dot', job.status)} />
          <span className={statusColors[job.status]}>{job.status}</span>
        </div>
      </td>
      <td className="px-4 py-3">
        <div className="font-medium">{job.item_name}</div>
        {job.notes && <div className="text-xs text-farm-500 truncate max-w-xs">{job.notes}</div>}
      </td>
      <td className="px-4 py-3">
        <span className={clsx(
          'px-2 py-0.5 rounded text-xs font-medium',
          job.priority <= 2 ? 'bg-red-900/50 text-red-400' : 
          job.priority >= 4 ? 'bg-farm-800 text-farm-400' : 
          'bg-amber-900/50 text-amber-400'
        )}>
          P{job.priority}
        </span>
      </td>
      <td className="px-4 py-3">{job.printer?.name || '—'}</td>
      <td className="px-4 py-3">
        {job.colors_list?.length > 0 ? (
          <div className="flex gap-1 flex-wrap">
            {job.colors_list.map((color, i) => (
              <span key={i} className="w-5 h-5 rounded-full border border-farm-700 flex items-center justify-center" style={{ backgroundColor: resolveColor(color) || "#333" }} title={color}>{!resolveColor(color) && <span className="text-[10px] text-farm-400">?</span>}</span>
            ))}
          </div>
        ) : '—'}
      </td>
      <td className="px-4 py-3 text-sm text-farm-400">{formatHours(job.duration_hours)}</td>
      <td className="px-4 py-3 text-sm text-farm-400">
        {job.scheduled_start ? format(new Date(job.scheduled_start), 'MMM d HH:mm') : '—'}
      </td>
      <td className="px-4 py-3">
        <div className="flex items-center gap-1">
          {job.status === 'scheduled' && (
            <button onClick={() => onAction('start', job.id)} className="p-1.5 text-print-400 hover:bg-print-900/50 rounded" title="Start Print">
              <Play size={16} />
            </button>
          )}
          {job.status === 'printing' && (
            <button onClick={() => onAction('complete', job.id)} className="p-1.5 text-green-400 hover:bg-green-900/50 rounded" title="Complete">
              <CheckCircle size={16} />
            </button>
          )}
          {(job.status === 'scheduled' || job.status === 'printing') && (
            <button onClick={() => onAction('cancel', job.id)} className="p-1.5 text-red-400 hover:bg-red-900/50 rounded" title="Cancel">
              <XCircle size={16} />
            </button>
          )}
          {(job.status === 'pending' || job.status === 'scheduled' || job.status === 'failed') && (
            <button onClick={() => onAction('delete', job.id)} className="p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded" title="Delete">
              <Trash2 size={16} />
            </button>
          )}
        </div>
      </td>
    </tr>
  )
}

function CreateJobModal({ isOpen, onClose, onSubmit, modelsData }) {
  const [formData, setFormData] = useState({
    item_name: '',
    model_id: '',
    priority: 3,
    duration_hours: '',
    colors_required: '',
    notes: '',
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    onSubmit({
      ...formData,
      model_id: formData.model_id ? Number(formData.model_id) : null,
      duration_hours: formData.duration_hours ? Number(formData.duration_hours) : null,
    })
    setFormData({ item_name: '', model_id: '', priority: 3, duration_hours: '', colors_required: '', notes: '' })
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
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-farm-900 rounded-xl w-full max-w-lg p-6 border border-farm-700">
        <h2 className="text-xl font-display font-semibold mb-4">Create New Job</h2>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Model (optional)</label>
            <select value={formData.model_id} onChange={(e) => handleModelSelect(e.target.value)} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2">
              <option value="">Select a model...</option>
              {modelsData?.map(model => (
                <option key={model.id} value={model.id}>{model.name}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Item Name *</label>
            <input type="text" required value={formData.item_name} onChange={(e) => setFormData(prev => ({ ...prev, item_name: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2" />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-farm-400 mb-1">Priority</label>
              <select value={formData.priority} onChange={(e) => setFormData(prev => ({ ...prev, priority: Number(e.target.value) }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2">
                {priorityOptions.map(opt => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm text-farm-400 mb-1">Duration (hours)</label>
              <input type="number" step="0.5" value={formData.duration_hours} onChange={(e) => setFormData(prev => ({ ...prev, duration_hours: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2" />
            </div>
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Colors Required</label>
            <input type="text" value={formData.colors_required} onChange={(e) => setFormData(prev => ({ ...prev, colors_required: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2" placeholder="e.g., black, white, green matte" />
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Notes</label>
            <textarea value={formData.notes} onChange={(e) => setFormData(prev => ({ ...prev, notes: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2" rows={2} />
          </div>
          <div className="flex justify-end gap-3 pt-4">
            <button type="button" onClick={onClose} className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg">Cancel</button>
            <button type="submit" className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg">Create Job</button>
          </div>
        </form>
      </div>
    </div>
  )
}

export default function Jobs() {
  const queryClient = useQueryClient()
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [statusFilter, setStatusFilter] = useState('')
  const [searchQuery, setSearchQuery] = useState('')

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

  const handleAction = (action, jobId) => {
    switch (action) {
      case 'start': startJob.mutate(jobId); break
      case 'complete': completeJob.mutate(jobId); break
      case 'cancel': cancelJob.mutate(jobId); break
      case 'delete': if (confirm('Delete this job?')) deleteJob.mutate(jobId); break
    }
  }

  const filteredJobs = jobsData?.filter(job => 
    !searchQuery || job.item_name.toLowerCase().includes(searchQuery.toLowerCase())
  ) || []

  return (
    <div className="p-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-display font-bold">Jobs</h1>
          <p className="text-farm-500 mt-1">Manage print queue</p>
        </div>
        <button onClick={() => setShowCreateModal(true)} className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg">
          <Plus size={18} /> New Job
        </button>
      </div>

      <div className="flex items-center gap-4 mb-6">
        <div className="relative flex-1 max-w-md">
          <Search size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500" />
          <input
            type="text"
            placeholder="Search jobs..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-10 pr-4 py-2 bg-farm-900 border border-farm-800 rounded-lg"
          />
        </div>
        <div className="flex items-center gap-2">
          <Filter size={18} className="text-farm-500" />
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} className="bg-farm-900 border border-farm-800 rounded-lg px-3 py-2">
            {statusOptions.map(opt => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="bg-farm-900 rounded-xl border border-farm-800 overflow-hidden">
        <table className="w-full">
          <thead className="bg-farm-950 border-b border-farm-800">
            <tr>
              <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Status</th>
              <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Item</th>
              <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Priority</th>
              <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Printer</th>
              <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Colors</th>
              <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Duration</th>
              <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Scheduled</th>
              <th className="px-4 py-3 text-left text-sm font-medium text-farm-400">Actions</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr><td colSpan={8} className="px-4 py-8 text-center text-farm-500">Loading...</td></tr>
            ) : filteredJobs.length === 0 ? (
              <tr><td colSpan={8} className="px-4 py-8 text-center text-farm-500">No jobs found</td></tr>
            ) : (
              filteredJobs.map(job => (
                <JobRow key={job.id} job={job} onAction={handleAction} />
              ))
            )}
          </tbody>
        </table>
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
