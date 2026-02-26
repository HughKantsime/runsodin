import { useState, useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle } from 'lucide-react'
import clsx from 'clsx'
import { modelRevisions } from '../../api'
import { priorityOptions } from './jobUtils'

function BedMismatchWarning({ modelId, printerId, modelsData, printersData }) {
  if (!modelId || !printerId || !modelsData || !printersData) return null
  const model = modelsData.find(m => m.id === Number(modelId))
  const printer = printersData.find(p => p.id === Number(printerId))
  if (!model || !printer) return null
  const modelX = model.bed_x_mm
  const modelY = model.bed_y_mm
  const printerX = printer.bed_x_mm
  const printerY = printer.bed_y_mm
  if (modelX == null || modelY == null || printerX == null || printerY == null) return null
  const TOLERANCE = 2
  if (modelX <= printerX + TOLERANCE && modelY <= printerY + TOLERANCE) return null
  return (
    <div className="flex items-start gap-2 p-3 bg-yellow-900/30 border border-yellow-700/40 rounded-lg text-xs text-yellow-300">
      <AlertTriangle size={14} className="mt-0.5 shrink-0 text-yellow-400" />
      <span>
        File sliced for {modelX}x{modelY}mm — this printer's bed is {printerX}x{printerY}mm.
        You can still dispatch but it will likely fail.
      </span>
    </div>
  )
}

export function CreateJobModal({ isOpen, onClose, onSubmit, onSavePreset, modelsData, printersData }) {
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
    target_type: 'specific',
    target_filter: '',
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
      target_type: formData.target_type || 'specific',
      target_filter: formData.target_filter || null,
    })
    setFormData({ item_name: '', model_id: '', model_revision_id: '', priority: 3, quantity: 1, duration_hours: '', colors_required: '', notes: '', due_date: '', required_tags: '', printer_id: '', target_type: 'specific', target_filter: '' })
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
            <label className="block text-sm text-farm-400 mb-1">Printer Target</label>
            <div className="flex gap-2 mb-2">
              {[
                { value: 'specific', label: 'Specific Printer' },
                { value: 'model', label: 'Any Model Type' },
                { value: 'protocol', label: 'Any Protocol' },
              ].map(opt => (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => setFormData(prev => ({ ...prev, target_type: opt.value, printer_id: '', target_filter: '' }))}
                  className={`px-3 py-1 rounded text-xs border transition-colors ${formData.target_type === opt.value ? 'border-print-500 bg-print-900/30 text-print-400' : 'border-farm-700 text-farm-400 hover:border-farm-500'}`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
            {formData.target_type === 'specific' && (
              <>
                <select value={formData.printer_id} onChange={(e) => setFormData(prev => ({ ...prev, printer_id: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
                  <option value="">Auto-assign</option>
                  {printersData?.map(p => (
                    <option key={p.id} value={p.id}>{p.nickname || p.name}</option>
                  ))}
                </select>
                <BedMismatchWarning modelId={formData.model_id} printerId={formData.printer_id} modelsData={modelsData} printersData={printersData} />
              </>
            )}
            {formData.target_type === 'model' && (
              <select value={formData.target_filter} onChange={(e) => setFormData(prev => ({ ...prev, target_filter: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
                <option value="">Select machine type...</option>
                {[...new Set((printersData || []).map(p => p.machine_type).filter(Boolean))].map(mt => (
                  <option key={mt} value={mt}>{mt}</option>
                ))}
              </select>
            )}
            {formData.target_type === 'protocol' && (
              <select value={formData.target_filter} onChange={(e) => setFormData(prev => ({ ...prev, target_filter: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm">
                <option value="">Select protocol...</option>
                {[...new Set((printersData || []).map(p => p.api_type).filter(Boolean))].map(pt => (
                  <option key={pt} value={pt}>{pt}</option>
                ))}
              </select>
            )}
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

export function RejectModal({ isOpen, onClose, onSubmit }) {
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

export function EditJobModal({ isOpen, onClose, onSubmit, job, printersData, modelsData }) {
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
              <BedMismatchWarning
                modelId={job?.model_id}
                printerId={formData.printer_id}
                modelsData={modelsData}
                printersData={printersData}
              />
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
