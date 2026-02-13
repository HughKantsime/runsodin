import { resolveColor } from "../utils/colorMap"

function formatHours(h) {
  if (!h) return "—"
  if (h < 1) return Math.round(h * 60) + "m"
  const hrs = Math.floor(h)
  const mins = Math.round((h - hrs) * 60)
  return mins > 0 ? hrs + "h " + mins + "m" : hrs + "h"
}
import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Play, CheckCircle, XCircle, RotateCcw, Trash2, Filter, Search, ArrowUp, ArrowDown, ArrowUpDown, ShoppingCart, Layers, Zap, RefreshCw, Clock, MessageSquare, AlertTriangle, Calendar, Flag, History, Pencil, GripVertical } from 'lucide-react'
import { format } from 'date-fns'
import clsx from 'clsx'
import { jobs, models, printers as printersApi, scheduler, approveJob, rejectJob, resubmitJob, getApprovalSetting, presets, bulkOps, modelRevisions } from '../api'
import { canDo } from '../permissions'
import { useOrg } from '../contexts/OrgContext'
import FailureReasonModal from '../components/FailureReasonModal'
import { updateJobFailure } from '../api'
import toast from 'react-hot-toast'
import ConfirmModal from '../components/ConfirmModal'

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

function JobRow({ job, onAction, dragProps, isSelected, onToggleSelect }) {
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
    <tr className={clsx(
          'border-b border-farm-800 hover:bg-farm-900/50',
          job.due_date && new Date(job.due_date) < new Date() && !['completed','cancelled','failed'].includes(job.status) && 'bg-red-950/30 border-l-2 border-l-red-500'
        )}
        {...(dragProps || {})}>
      <td className="px-2 py-3 w-10">
        <div className="flex items-center gap-1">
          {dragProps && (
            <GripVertical size={14} className="text-farm-600 flex-shrink-0 cursor-grab" title="Drag to reorder" />
          )}
          <input type="checkbox" checked={isSelected} onChange={() => onToggleSelect(job.id)} className="rounded border-farm-600" aria-label={`Select job ${job.item_name}`} />
        </div>
      </td>
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
          'px-2 py-0.5 rounded-lg text-xs font-medium',
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
      <td className="px-3 md:px-4 py-3 text-sm text-farm-400 hidden md:table-cell">
        <span>{formatHours(job.duration_hours)}</span>
        {job.actual_start && job.actual_end && (() => {
          const actualH = (new Date(job.actual_end) - new Date(job.actual_start)) / 3600000
          return (
            <span className="block text-xs text-farm-500" title="Actual duration">
              {formatHours(actualH)} actual
            </span>
          )
        })()}
      </td>
      <td className="px-3 md:px-4 py-3 text-sm text-farm-400 hidden lg:table-cell">
        {job.scheduled_start ? format(new Date(job.scheduled_start), 'MMM d HH:mm') : '—'}
      </td>
      <td className="px-3 md:px-4 py-3">
        <div className="flex items-center gap-1">
          {job.status === 'submitted' && canDo('jobs.approve') && (
            <>
              <button onClick={() => onAction('approve', job.id)} className="p-1.5 text-green-400 hover:bg-green-900/50 rounded-lg" aria-label="Approve job">
                <CheckCircle size={16} />
              </button>
              <button onClick={() => onAction('reject', job.id)} className="p-1.5 text-red-400 hover:bg-red-900/50 rounded-lg" aria-label="Reject job">
                <XCircle size={16} />
              </button>
            </>
          )}
          {job.status === 'rejected' && job.submitted_by && canDo('jobs.resubmit') && (
            <button onClick={() => onAction('resubmit', job.id)} className="p-1.5 text-amber-400 hover:bg-amber-900/50 rounded-lg" aria-label="Resubmit job">
              <RefreshCw size={14} />
            </button>
          )}
          {job.status === 'scheduled' && canDo('jobs.start') && (
            <button onClick={() => onAction('start', job.id)} className="p-1.5 text-print-400 hover:bg-print-900/50 rounded-lg" aria-label="Start print">
              <Play size={16} />
            </button>
          )}
          {job.status === 'printing' && canDo('jobs.complete') && (
            <>
              <button onClick={() => onAction('complete', job.id)} className="p-1.5 text-green-400 hover:bg-green-900/50 rounded-lg" aria-label="Mark job complete">
                <CheckCircle size={16} />
              </button>
              <button onClick={() => onAction('markFailed', job.id, job.item_name)} className="p-1.5 text-red-400 hover:bg-red-900/50 rounded-lg" aria-label="Mark job failed">
                <AlertTriangle size={16} />
              </button>
            </>
          )}
          {canDo('jobs.edit') && ['pending', 'scheduled', 'submitted'].includes(job.status) && (
            <button onClick={() => onAction('edit', job.id)} className="p-1.5 text-farm-400 hover:text-print-400 hover:bg-print-900/50 rounded-lg" aria-label="Edit job">
              <Pencil size={14} />
            </button>
          )}
          {canDo('jobs.cancel') && (job.status === 'scheduled' || job.status === 'printing') && (
            <button onClick={() => onAction('cancel', job.id)} className="p-1.5 text-red-400 hover:bg-red-900/50 rounded-lg" aria-label="Cancel job">
              <XCircle size={16} />
            </button>
          )}
          {job.status === 'failed' && (
            <button onClick={() => onAction('failReason', job.id, job.item_name, job.fail_reason, job.fail_notes)} className="p-1.5 text-amber-400 hover:bg-amber-900/50 rounded-lg" aria-label={job.fail_reason ? 'Edit failure reason' : 'Add failure reason'}>
              <AlertTriangle size={16} />
            </button>
          )}
          {canDo('jobs.delete') && (job.status === 'pending' || job.status === 'scheduled' || job.status === 'failed') && (
            <>
              <button onClick={() => onAction('repeat', job.id)} className="p-1.5 text-farm-400 hover:text-print-400 hover:bg-print-900/50 rounded-lg" aria-label="Print again">
                <RefreshCw size={14} />
              </button>
              <button onClick={() => onAction('delete', job.id)} className="p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded-lg" aria-label="Delete job">
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

function CreateJobModal({ isOpen, onClose, onSubmit, onSavePreset, modelsData, printersData }) {
  const [formData, setFormData] = useState({
    item_name: '',
    model_id: '',
    model_revision_id: '',
    priority: 3,
    quantity: 1,
    duration_hours: '',
    colors_required: '',
    notes: '',
    due_date: '',
    required_tags: '',
    printer_id: '',
  })
  const [presetName, setPresetName] = useState('')
  const [showPresetInput, setShowPresetInput] = useState(false)

  const { data: revisions } = useQuery({
    queryKey: ['model-revisions', formData.model_id],
    queryFn: () => modelRevisions.list(Number(formData.model_id)),
    enabled: !!formData.model_id,
  })

  useEffect(() => {
    if (!isOpen) return
    const handleKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [isOpen, onClose])

  const handleSubmit = (e) => {
    e.preventDefault()
    const tags = formData.required_tags ? formData.required_tags.split(',').map(t => t.trim()).filter(Boolean) : []
    onSubmit({
      ...formData,
      model_id: formData.model_id ? Number(formData.model_id) : null,
      model_revision_id: formData.model_revision_id ? Number(formData.model_revision_id) : null,
      duration_hours: formData.duration_hours ? Number(formData.duration_hours) : null,
      quantity: formData.quantity ? Number(formData.quantity) : 1,
      due_date: formData.due_date || null,
      required_tags: tags,
      printer_id: formData.printer_id ? Number(formData.printer_id) : null,
    })
    setFormData({ item_name: '', model_id: '', model_revision_id: '', priority: 3, quantity: 1, duration_hours: '', colors_required: '', notes: '', due_date: '', required_tags: '', printer_id: '' })
    onClose()
  }

  const handleModelSelect = (modelId) => {
    const model = modelsData?.find(m => m.id === Number(modelId))
    setFormData(prev => ({
      ...prev,
      model_id: modelId,
      model_revision_id: '',
      item_name: model?.name || prev.item_name,
      duration_hours: model?.build_time_hours || prev.duration_hours,
      colors_required: model?.required_colors?.join(', ') || prev.colors_required,
    }))
  }

  if (!isOpen) return null

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4" role="dialog" aria-modal="true" aria-labelledby="create-job-title" onClick={onClose}>
      <div className="bg-farm-900 rounded-t-xl sm:rounded w-full max-w-lg p-4 sm:p-6 border border-farm-700 max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <h2 id="create-job-title" className="text-lg sm:text-xl font-display font-semibold mb-4">Create New Job</h2>
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
          {formData.model_id && revisions?.length > 0 && (
            <div>
              <label className="block text-sm text-farm-400 mb-1">Revision</label>
              <select value={formData.model_revision_id} onChange={(e) => setFormData(prev => ({ ...prev, model_revision_id: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
                <option value="">Latest</option>
                {revisions.map(rev => (
                  <option key={rev.id} value={rev.id}>v{rev.revision_number}{rev.changelog ? ` — ${rev.changelog}` : ''}</option>
                ))}
              </select>
            </div>
          )}
          <div>
            <label className="block text-sm text-farm-400 mb-1">Item Name *</label>
            <input type="text" required value={formData.item_name} onChange={(e) => setFormData(prev => ({ ...prev, item_name: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div>
              <label className="block text-sm text-farm-400 mb-1">Priority</label>
              <select value={formData.priority} onChange={(e) => setFormData(prev => ({ ...prev, priority: Number(e.target.value) }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
                {priorityOptions.map(opt => (
                  <option key={opt.value} value={opt.value}>{opt.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm text-farm-400 mb-1">Quantity</label>
              <input type="number" min="1" value={formData.quantity} onChange={(e) => setFormData(prev => ({ ...prev, quantity: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
            </div>
            <div>
              <label className="block text-sm text-farm-400 mb-1">Duration (hours)</label>
              <input type="number" step="0.5" value={formData.duration_hours} onChange={(e) => setFormData(prev => ({ ...prev, duration_hours: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
            </div>
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Assign Printer (optional)</label>
            <select value={formData.printer_id} onChange={(e) => setFormData(prev => ({ ...prev, printer_id: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
              <option value="">Auto-assign</option>
              {printersData?.map(p => (
                <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
              ))}
            </select>
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
            <label className="block text-sm text-farm-400 mb-1">Printer Tags (optional)</label>
            <input type="text" value={formData.required_tags} onChange={(e) => setFormData(prev => ({ ...prev, required_tags: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="e.g., Room A, Production" />
            <p className="text-xs text-farm-500 mt-0.5">Only schedule on printers with these tags</p>
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Notes</label>
            <textarea value={formData.notes} onChange={(e) => setFormData(prev => ({ ...prev, notes: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" rows={2} />
          </div>
          <div className="flex justify-end gap-3 pt-4">
            <button type="button" onClick={onClose} className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm">Cancel</button>
            {onSavePreset && formData.item_name && !showPresetInput && (
              <button
                type="button"
                onClick={() => { setPresetName(formData.item_name); setShowPresetInput(true) }}
                className="px-4 py-2 bg-amber-600 hover:bg-amber-500 rounded-lg text-sm"
              >
                Save Preset
              </button>
            )}
            {showPresetInput && (
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={presetName}
                  onChange={(e) => setPresetName(e.target.value)}
                  className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm w-40"
                  placeholder="Preset name"
                  autoFocus
                  onKeyDown={(e) => {
                    if (e.key === 'Escape') { setShowPresetInput(false); setPresetName('') }
                    if (e.key === 'Enter') {
                      e.preventDefault()
                      if (presetName.trim()) {
                        const tags = formData.required_tags ? formData.required_tags.split(',').map(t => t.trim()).filter(Boolean) : []
                        onSavePreset({
                          name: presetName.trim(),
                          model_id: formData.model_id ? Number(formData.model_id) : null,
                          item_name: formData.item_name,
                          priority: formData.priority,
                          duration_hours: formData.duration_hours ? Number(formData.duration_hours) : null,
                          colors_required: formData.colors_required || null,
                          required_tags: tags,
                          notes: formData.notes || null,
                        })
                        setShowPresetInput(false)
                        setPresetName('')
                      }
                    }
                  }}
                />
                <button
                  type="button"
                  onClick={() => {
                    if (presetName.trim()) {
                      const tags = formData.required_tags ? formData.required_tags.split(',').map(t => t.trim()).filter(Boolean) : []
                      onSavePreset({
                        name: presetName.trim(),
                        model_id: formData.model_id ? Number(formData.model_id) : null,
                        item_name: formData.item_name,
                        priority: formData.priority,
                        duration_hours: formData.duration_hours ? Number(formData.duration_hours) : null,
                        colors_required: formData.colors_required || null,
                        required_tags: tags,
                        notes: formData.notes || null,
                      })
                      setShowPresetInput(false)
                      setPresetName('')
                    }
                  }}
                  className="px-3 py-2 bg-amber-600 hover:bg-amber-500 rounded-lg text-sm"
                >
                  Save
                </button>
              </div>
            )}
            <button type="submit" className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-sm">Create Job</button>
          </div>
        </form>
      </div>
    </div>
  )
}


function RejectModal({ isOpen, onClose, onSubmit }) {
  const [reason, setReason] = useState('')

  useEffect(() => {
    if (!isOpen) return
    const handleKey = (e) => { if (e.key === 'Escape') { onClose(); setReason('') } }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [isOpen, onClose])

  if (!isOpen) return null
  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4" role="dialog" aria-modal="true" aria-labelledby="reject-job-title" onClick={() => { onClose(); setReason('') }}>
      <div className="bg-farm-900 rounded-t-xl sm:rounded w-full max-w-md p-4 sm:p-6 border border-farm-700" onClick={e => e.stopPropagation()}>
        <h2 id="reject-job-title" className="text-lg font-display font-semibold mb-4">Reject Job</h2>
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

function EditJobModal({ isOpen, onClose, onSubmit, job, printersData }) {
  const [formData, setFormData] = useState({})

  useEffect(() => {
    if (job) {
      setFormData({
        item_name: job.item_name || '',
        quantity: job.quantity || 1,
        priority: job.priority || 3,
        duration_hours: job.duration_hours || '',
        colors_required: job.colors_list?.join(', ') || '',
        filament_type: job.filament_type || '',
        notes: job.notes || '',
        due_date: job.due_date ? job.due_date.split('T')[0] : '',
        printer_id: job.printer_id || '',
      })
    }
  }, [job])

  useEffect(() => {
    if (!isOpen) return
    const handleKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [isOpen, onClose])

  const handleSubmit = (e) => {
    e.preventDefault()
    onSubmit(job.id, {
      item_name: formData.item_name,
      quantity: formData.quantity ? Number(formData.quantity) : null,
      priority: Number(formData.priority),
      duration_hours: formData.duration_hours ? Number(formData.duration_hours) : null,
      colors_required: formData.colors_required || null,
      filament_type: formData.filament_type || null,
      notes: formData.notes || null,
      due_date: formData.due_date || null,
      printer_id: formData.printer_id ? Number(formData.printer_id) : null,
    })
    onClose()
  }

  if (!isOpen || !job) return null

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4" role="dialog" aria-modal="true" aria-labelledby="edit-job-title" onClick={onClose}>
      <div className="bg-farm-900 rounded-t-xl sm:rounded w-full max-w-lg p-4 sm:p-6 border border-farm-700 max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <h2 id="edit-job-title" className="text-lg sm:text-xl font-display font-semibold mb-4">Edit Job</h2>
        <form onSubmit={handleSubmit} className="space-y-4">
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
              <label className="block text-sm text-farm-400 mb-1">Quantity</label>
              <input type="number" min="1" value={formData.quantity} onChange={(e) => setFormData(prev => ({ ...prev, quantity: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-farm-400 mb-1">Duration (hours)</label>
              <input type="number" step="0.5" value={formData.duration_hours} onChange={(e) => setFormData(prev => ({ ...prev, duration_hours: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
            </div>
            <div>
              <label className="block text-sm text-farm-400 mb-1">Assign Printer</label>
              <select value={formData.printer_id} onChange={(e) => setFormData(prev => ({ ...prev, printer_id: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
                <option value="">Unassigned</option>
                {printersData?.map(p => (
                  <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
                ))}
              </select>
            </div>
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Colors Required</label>
            <input type="text" value={formData.colors_required} onChange={(e) => setFormData(prev => ({ ...prev, colors_required: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="e.g., black, white, green matte" />
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Due Date</label>
            <input type="date" value={formData.due_date} onChange={(e) => setFormData(prev => ({ ...prev, due_date: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Notes</label>
            <textarea value={formData.notes} onChange={(e) => setFormData(prev => ({ ...prev, notes: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" rows={2} />
          </div>
          <div className="flex justify-end gap-3 pt-4">
            <button type="button" onClick={onClose} className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm">Cancel</button>
            <button type="submit" className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-sm">Save Changes</button>
          </div>
        </form>
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
  const org = useOrg()
  const queryClient = useQueryClient()
  const runScheduler = useMutation({
    mutationFn: scheduler.run,
    onSuccess: () => {
      queryClient.invalidateQueries(['jobs'])
      queryClient.invalidateQueries(['stats'])
      toast.success('Scheduler run complete')
    },
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
  const toggleJobSelect = (id) => setSelectedJobs(prev => {
    const next = new Set(prev)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })
  const toggleSelectAll = (jobIds) => {
    setSelectedJobs(prev => prev.size === jobIds.length ? new Set() : new Set(jobIds))
  }
  const bulkAction = useMutation({
    mutationFn: ({ action, extra }) => bulkOps.jobs([...selectedJobs], action, extra),
    onSuccess: (_, vars) => {
      queryClient.invalidateQueries(['jobs'])
      setSelectedJobs(new Set())
      toast.success(`Bulk ${vars.action} completed`)
    },
    onError: (err, vars) => toast.error(`Bulk ${vars.action} failed: ${err.message}`),
  })
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
      toast.error('Reorder failed: ' + (err.message || 'Unknown error'))
    }
  }

  const { data: approvalSetting } = useQuery({
    queryKey: ['approval-setting'],
    queryFn: getApprovalSetting,
  })

  const { data: jobsData, isLoading } = useQuery({
    queryKey: ['jobs', statusFilter, org.orgId],
    queryFn: () => jobs.list(statusFilter || null, org.orgId),
  })

  const { data: modelsData } = useQuery({
    queryKey: ['models', org.orgId],
    queryFn: () => models.list(org.orgId),
  })

  const { data: printersData } = useQuery({
    queryKey: ['printers'],
    queryFn: () => printersApi.list(),
  })

  const { data: presetsData } = useQuery({
    queryKey: ['presets'],
    queryFn: () => presets.list(),
  })

  const schedulePreset = useMutation({
    mutationFn: presets.schedule,
    onSuccess: () => { queryClient.invalidateQueries(['jobs']); toast.success('Preset scheduled') },
    onError: (err) => toast.error('Schedule preset failed: ' + err.message),
  })

  const deletePreset = useMutation({
    mutationFn: presets.delete,
    onSuccess: () => { queryClient.invalidateQueries(['presets']); toast.success('Preset deleted') },
    onError: (err) => toast.error('Delete preset failed: ' + err.message),
  })

  const createPreset = useMutation({
    mutationFn: presets.create,
    onSuccess: () => { queryClient.invalidateQueries(['presets']); toast.success('Preset saved') },
    onError: (err) => toast.error('Save preset failed: ' + err.message),
  })

  const updateJob = useMutation({
    mutationFn: ({ id, data }) => jobs.update(id, data),
    onSuccess: () => { queryClient.invalidateQueries(['jobs']); toast.success('Job updated') },
    onError: (err) => toast.error('Update job failed: ' + err.message),
  })

  const createJob = useMutation({
    mutationFn: jobs.create,
    onSuccess: () => { queryClient.invalidateQueries(['jobs']); toast.success('Job created') },
    onError: (err) => toast.error('Create job failed: ' + err.message),
  })

  const startJob = useMutation({
    mutationFn: jobs.start,
    onSuccess: () => { queryClient.invalidateQueries(['jobs']); toast.success('Job started') },
    onError: (err) => toast.error('Start job failed: ' + err.message),
  })

  const completeJob = useMutation({
    mutationFn: jobs.complete,
    onSuccess: () => { queryClient.invalidateQueries(['jobs']); toast.success('Job completed') },
    onError: (err) => toast.error('Complete job failed: ' + err.message),
  })

  const cancelJob = useMutation({
    mutationFn: jobs.cancel,
    onSuccess: () => { queryClient.invalidateQueries(['jobs']); toast.success('Job cancelled') },
    onError: (err) => toast.error('Cancel job failed: ' + err.message),
  })

  const deleteJob = useMutation({
    mutationFn: jobs.delete,
    onSuccess: () => { queryClient.invalidateQueries(['jobs']); toast.success('Job deleted') },
    onError: (err) => toast.error('Delete job failed: ' + err.message),
  })

  const repeatJob = useMutation({
    mutationFn: async (jobId) => {
      return jobs.repeat(jobId)
    },
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['jobs'] }); toast.success('Job duplicated') },
    onError: (err) => toast.error('Repeat job failed: ' + err.message),
  })

  const approveJobMut = useMutation({
    mutationFn: approveJob,
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['jobs'] }); toast.success('Job approved') },
    onError: (err) => toast.error('Approve job failed: ' + err.message),
  })

  const rejectJobMut = useMutation({
    mutationFn: ({ jobId, reason }) => rejectJob(jobId, reason),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['jobs'] }); toast.success('Job rejected') },
    onError: (err) => toast.error('Reject job failed: ' + err.message),
  })

  const resubmitJobMut = useMutation({
    mutationFn: resubmitJob,
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['jobs'] }); toast.success('Job resubmitted') },
    onError: (err) => toast.error('Resubmit job failed: ' + err.message),
  })

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
            <thead className="bg-farm-950 border-b border-farm-800">
              <tr>
                <th scope="col" className="px-2 py-3 w-10">
                  <input type="checkbox" checked={filteredJobs.length > 0 && selectedJobs.size === filteredJobs.length} onChange={() => toggleSelectAll(filteredJobs.map(j => j.id))} className="rounded border-farm-600" aria-label="Select all jobs" />
                </th>
                <th scope="col" className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400 cursor-pointer hover:text-farm-200 select-none" onClick={() => toggleSort('status')}>
                  <div className="flex items-center gap-1">Status <SortIcon field="status" sortField={sortField} sortDirection={sortDirection} /></div>
                </th>
                <th scope="col" className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400 cursor-pointer hover:text-farm-200 select-none" onClick={() => toggleSort('item_name')}>
                  <div className="flex items-center gap-1">Item <SortIcon field="item_name" sortField={sortField} sortDirection={sortDirection} /></div>
                </th>
                <th scope="col" className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400 cursor-pointer hover:text-farm-200 select-none" onClick={() => toggleSort('priority')}>
                  <div className="flex items-center gap-1">Pri <SortIcon field="priority" sortField={sortField} sortDirection={sortDirection} /></div>
                </th>
                <th scope="col" className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400 cursor-pointer hover:text-farm-200 select-none" onClick={() => toggleSort('printer')}>
                  <div className="flex items-center gap-1">Printer <SortIcon field="printer" sortField={sortField} sortDirection={sortDirection} /></div>
                </th>
                <th scope="col" className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400 hidden lg:table-cell">Colors</th>
                <th scope="col" className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400 hidden md:table-cell cursor-pointer hover:text-farm-200 select-none" onClick={() => toggleSort('duration_hours')}>
                  <div className="flex items-center gap-1">Duration <SortIcon field="duration_hours" sortField={sortField} sortDirection={sortDirection} /></div>
                </th>
                <th scope="col" className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400 hidden lg:table-cell cursor-pointer hover:text-farm-200 select-none" onClick={() => toggleSort('scheduled_start')}>
                  <div className="flex items-center gap-1">Scheduled <SortIcon field="scheduled_start" sortField={sortField} sortDirection={sortDirection} /></div>
                </th>
                <th scope="col" className="px-3 md:px-4 py-3 text-left text-xs font-medium text-farm-400">Actions</th>
              </tr>
            </thead>
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
                  <JobRow key={job.id} job={job} onAction={handleAction} isSelected={selectedJobs.has(job.id)} onToggleSelect={toggleJobSelect}
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
