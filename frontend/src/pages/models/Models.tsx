import { ModelCard, ColorSlotInput } from '../../components/models/ModelLibraryFields'
import { useState, useEffect, useRef, useCallback, lazy, Suspense } from 'react'
import { useSearchParams, Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Plus,
  Pencil,
  Trash2,
  Clock,
  Palette,
  DollarSign,
  Scale,
  X,
  ChevronDown,
  CalendarPlus,
  Printer as PrinterIcon,
  Star, Box, History, Upload, AlertTriangle, Package } from 'lucide-react'
const ModelViewer = lazy(() => import('../../components/models/ModelViewer'))
import ModelRevisionPanel from '../../components/models/ModelRevisionPanel'
import clsx from 'clsx'
import toast from 'react-hot-toast'
import ConfirmModal from '../../components/shared/ConfirmModal'
import { PageHeader, SearchInput, StatCard, TabBar, Button, EmptyState, Modal } from '../../components/ui'

import { models, filaments, printers, getApprovalSetting } from '../../api'
import { canDo } from '../../permissions'
import { useOrg } from '../../contexts/OrgContext'

function ModelModal({ isOpen, onClose, onSubmit, editingModel }) {
  const [formData, setFormData] = useState({ name: '', category: '', build_time_hours: '', cost_per_item: '', notes: '', units_per_bed: 1, markup_percent: 300 })
  const [colorSlots, setColorSlots] = useState([{ color: '', grams: '' }])

  const { data: filamentData } = useQuery({ queryKey: ['filaments-combined'], queryFn: filaments.combined, enabled: isOpen })

  useEffect(() => {
    if (editingModel) {
      setFormData({ name: editingModel.name || '', category: editingModel.category || '', build_time_hours: editingModel.build_time_hours || '', cost_per_item: editingModel.cost_per_item || '', notes: editingModel.notes || '', units_per_bed: editingModel.units_per_bed || 1, markup_percent: editingModel.markup_percent || 300 })
      if (editingModel.color_requirements && Object.keys(editingModel.color_requirements).length > 0) {
        const slots = Object.values(editingModel.color_requirements).map(c => ({ color: c.color || '', grams: c.grams || '' }))
        setColorSlots(slots.length > 0 ? slots : [{ color: '', grams: '' }])
      } else if (editingModel.required_colors?.length > 0) {
        setColorSlots(editingModel.required_colors.map(c => ({ color: c, grams: '' })))
      } else {
        setColorSlots([{ color: '', grams: '' }])
      }
    } else {
      setFormData({ name: '', category: '', build_time_hours: '', cost_per_item: '', notes: '', units_per_bed: 1, markup_percent: 300 })
      setColorSlots([{ color: '', grams: '' }])
    }
  }, [editingModel])

  const handleColorChange = (index, field, value) => {
    setColorSlots(prev => { const updated = [...prev]; updated[index] = { ...updated[index], [field]: value }; return updated })
  }
  const addColorSlot = () => {
    if (colorSlots.length >= 4) {
      toast.error('Maximum 4 color slots allowed')
      return
    }
    setColorSlots(prev => [...prev, { color: '', grams: '' }])
  }
  const removeColorSlot = (index) => { setColorSlots(prev => prev.filter((_, i) => i !== index)) }
  const totalFilament = colorSlots.reduce((sum, slot) => sum + (parseFloat(slot.grams) || 0), 0)

  const handleSubmit = (e) => {
    e.preventDefault()
    const color_requirements = {}
    colorSlots.forEach((slot, i) => {
      if (slot.color.trim()) {
        color_requirements[`color${i + 1}`] = { color: slot.color.trim(), grams: parseFloat(slot.grams) || 0 }
      }
    })
    onSubmit({
      name: formData.name, category: formData.category || null, build_time_hours: formData.build_time_hours ? Number(formData.build_time_hours) : null, cost_per_item: formData.cost_per_item ? Number(formData.cost_per_item) : null, notes: formData.notes || null, color_requirements: Object.keys(color_requirements).length > 0 ? color_requirements : null, units_per_bed: formData.units_per_bed ? Number(formData.units_per_bed) : 1, markup_percent: formData.markup_percent ? Number(formData.markup_percent) : 300,
    }, editingModel?.id)
    setFormData({ name: '', category: '', build_time_hours: '', cost_per_item: '', notes: '', units_per_bed: 1, markup_percent: 300 })
    setColorSlots([{ color: '', grams: '' }])
    onClose()
  }

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={editingModel ? 'Edit Model' : 'Add New Model'} size="lg">
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm text-[var(--brand-muted)] mb-1">Model Name *</label>
          <input type="text" required value={formData.name} onChange={(e) => setFormData(prev => ({ ...prev, name: e.target.value }))} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm" placeholder="e.g., Crocodile (Mini Critter)" />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-sm text-[var(--brand-muted)] mb-1">Category</label>
            <input type="text" value={formData.category} onChange={(e) => setFormData(prev => ({ ...prev, category: e.target.value }))} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm" placeholder="e.g., Mini Critters" />
          </div>
          <div>
            <label className="block text-sm text-[var(--brand-muted)] mb-1">Build Time (hours)</label>
            <input type="number" step="any" min="0" value={formData.build_time_hours} onChange={(e) => setFormData(prev => ({ ...prev, build_time_hours: e.target.value }))} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm" placeholder="e.g., 15.5" />
          </div>
        </div>
        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="block text-sm text-[var(--brand-muted)]">Filament Colors & Usage</label>
            {colorSlots.length < 4 ? (
              <button type="button" onClick={addColorSlot} className="text-xs text-[var(--brand-primary)] hover:text-[var(--brand-primary)] flex items-center gap-1">
                <Plus size={14} /> Add Color
              </button>
            ) : (
              <span className="text-xs text-[var(--brand-muted)]">Max 4 colors</span>
            )}
          </div>
          <div className="space-y-2">
            {colorSlots.map((slot, i) => (
              <ColorSlotInput key={i} index={i} color={slot.color} grams={slot.grams} onChange={handleColorChange} onRemove={removeColorSlot} filamentData={filamentData} />
            ))}
          </div>
          {totalFilament > 0 && (
            <div className="mt-2 text-sm text-[var(--brand-muted)] flex items-center gap-2">
              <Scale size={14} /> Total: <span className="text-white font-medium">{totalFilament.toFixed(1)}g</span>
            </div>
          )}
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-sm text-[var(--brand-muted)] mb-1">Cost per Item ($)</label>
            <input type="number" step="0.01" min="0" value={formData.cost_per_item} onChange={(e) => setFormData(prev => ({ ...prev, cost_per_item: e.target.value }))} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm" placeholder="e.g., 5.50" />
          </div>
          <div>
            <label className="block text-sm text-[var(--brand-muted)] mb-1"># on Bed</label>
            <input type="number" min="1" value={formData.units_per_bed} onChange={(e) => setFormData(prev => ({ ...prev, units_per_bed: e.target.value }))} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm" />
          </div>
        </div>
        <div>
          <label className="block text-sm text-[var(--brand-muted)] mb-1">Markup %</label>
          <input type="number" min="0" value={formData.markup_percent} onChange={(e) => setFormData(prev => ({ ...prev, markup_percent: e.target.value }))} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm" />
        </div>
        <div>
          <label className="block text-sm text-[var(--brand-muted)] mb-1">Notes</label>
          <textarea value={formData.notes} onChange={(e) => setFormData(prev => ({ ...prev, notes: e.target.value }))} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 h-20 text-sm" placeholder="Optional notes..." />
        </div>
        <div className="flex justify-end gap-3 pt-4">
          <button type="button" onClick={onClose} className="px-4 py-2 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md transition-colors text-sm">Cancel</button>
          <button type="submit" className="px-4 py-2 bg-[var(--brand-primary)] hover:bg-[var(--brand-primary-hover)] rounded-md transition-colors text-sm">{editingModel ? 'Save Changes' : 'Add Model'}</button>
        </div>
      </form>
    </Modal>
  )
}

function ScheduleModal({ isOpen, onClose, model, onConfirm, isScheduling }) {
  const { data: printersData } = useQuery({
    queryKey: ['printers'],
    queryFn: () => printers.list(true),
    enabled: isOpen
  })
  const { data: variantsData } = useQuery({
    queryKey: ['model-variants', model?.id],
    queryFn: () => models.getVariants(model.id),
    enabled: isOpen && !!model?.id
  })
  const { data: payloadOptions, isLoading: loadingPayload, error: payloadError } = useQuery({
    queryKey: ['model-schedule-options', model?.id],
    queryFn: () => models.scheduleOptions(model.id),
    enabled: isOpen && !!model?.id,
    staleTime: 0,
  })
  const { data: plateData, isLoading: loadingPlates, error: plateError } = useQuery({
    queryKey: ['model-plates', model?.id], queryFn: () => models.plates(model.id), enabled: isOpen && !!model?.id, staleTime: 0,
  })
  useEffect(() => {
    const indices = payloadOptions?.plate_indices || []
    if (indices.length) setScheduleOpts(prev => ({ ...prev, plate_index: indices.includes(prev.plate_index) ? prev.plate_index : indices[0] }))
  }, [payloadOptions])
  const boundPrinter = plateData?.target_printer_id
  const [selectedPrinter, setSelectedPrinter] = useState(null)
  const [scheduleOpts, setScheduleOpts] = useState({ priority: 3, quantity: 1, due_date: '', notes: '', plate_index: null })

  useEffect(() => {
    if (isOpen) {
      setSelectedPrinter(null)
      setScheduleOpts({ priority: 3, quantity: 1, due_date: '', notes: '', plate_index: model?.selectedPlateIndex ?? null })
    }
  }, [isOpen, model?.id, model?.selectedPlateIndex])

  useEffect(() => {
    if (isOpen && boundPrinter) setSelectedPrinter(boundPrinter)
  }, [boundPrinter, isOpen, model?.id])
  const activePrinters = printersData?.filter(p => p.is_active && (!boundPrinter || p.id === boundPrinter)) || []
  const variants = variantsData?.variants || []
  const variantProfiles = new Set(variants.map(v => v.printer_model?.toLowerCase()).filter(p => p && p !== 'unknown'))
  const hasMatch = (printer) => {
    if (!printer.model) return false
    const pm = printer.model.toLowerCase()
    for (const profile of variantProfiles) {
      if (pm.includes(profile)) return true
    }
    return false
  }
  const sortedPrinters = [...activePrinters].sort((a,b) => hasMatch(b) - hasMatch(a))

  if (!model) return null

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Schedule Print" size="md">
      {(payloadError || plateError) && <p role="alert">Unable to load scheduling details. Refresh and try again.</p>}
      <p className="text-sm text-[var(--brand-muted)] mb-2">{model.name}</p>
      {boundPrinter && <p className="text-sm mb-2">Bound to printer {boundPrinter}</p>}

        {variants.length > 0 && (
          <div className="flex flex-col gap-1.5 mb-3">
            {variants.map(v => (
              <div key={v.id} className="flex flex-wrap items-center gap-1">
                <span className="px-2 py-0.5 bg-blue-500/20 text-blue-300 rounded-md text-xs">{v.printer_model}</span>
                {v.bed_x_mm != null && v.bed_y_mm != null && (
                  <span className="px-2 py-0.5 bg-[var(--brand-card-border)]/60 text-[var(--brand-text-secondary)] rounded-md text-xs">{v.bed_x_mm}x{v.bed_y_mm}mm</span>
                )}
                {v.compatible_api_types === 'bambu' ? (
                  <span className="px-2 py-0.5 bg-blue-600/20 text-blue-400 rounded-md text-xs">Bambu only</span>
                ) : v.compatible_api_types ? (
                  <span className="px-2 py-0.5 bg-green-600/20 text-green-400 rounded-md text-xs">Moonraker / PrusaLink</span>
                ) : null}
                {v.bed_x_mm == null && !v.compatible_api_types && (
                  <span title="No slicer metadata — re-upload to enable bed checks" className="text-yellow-500 flex items-center">
                    <AlertTriangle size={12} />
                  </span>
                )}
              </div>
            ))}
          </div>
        )}

        {model.thumbnail_b64 && (
          <div className="h-32 bg-[var(--brand-content-bg)] rounded-md mb-4 flex items-center justify-center overflow-hidden">
            <img src={`data:image/png;base64,${model.thumbnail_b64}`} alt={model.name} className="h-full object-contain" />
          </div>
        )}

        <div className="flex items-center gap-4 text-sm text-[var(--brand-muted)] mb-4">
          {model.build_time_hours && (
            <div className="flex items-center gap-1">
              <Clock size={14} />
              <span>{model.build_time_hours}h</span>
            </div>
          )}
          {model.total_filament_grams > 0 && (
            <div className="flex items-center gap-1">
              <Scale size={14} />
              <span>{model.total_filament_grams}g</span>
            </div>
          )}
          {model.default_filament_type && (
            <div className="flex items-center gap-1">
              <Palette size={14} />
              <span>{model.default_filament_type}</span>
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3 mb-4">
          <div>
            <label className="block text-xs text-[var(--brand-muted)] mb-1">Priority</label>
            <select value={scheduleOpts.priority} onChange={(e) => setScheduleOpts(prev => ({ ...prev, priority: Number(e.target.value) }))} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-1.5 text-sm">
              <option value={1}>1 - Highest</option>
              <option value={2}>2 - High</option>
              <option value={3}>3 - Normal</option>
              <option value={4}>4 - Low</option>
              <option value={5}>5 - Lowest</option>
            </select>
          </div>
          <div>
            <label className="block text-xs text-[var(--brand-muted)] mb-1">Quantity</label>
            <input type="number" min="1" value={scheduleOpts.quantity} onChange={(e) => setScheduleOpts(prev => ({ ...prev, quantity: e.target.value }))} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-1.5 text-sm" />
          </div>
        </div>
        <div className="mb-4">
          <label className="block text-xs text-[var(--brand-muted)] mb-1">Due Date (optional)</label>
          <input type="date" value={scheduleOpts.due_date} onChange={(e) => setScheduleOpts(prev => ({ ...prev, due_date: e.target.value }))} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-1.5 text-sm" />
        </div>
        <div className="mb-4">
          <label className="block text-xs text-[var(--brand-muted)] mb-1">Notes (optional)</label>
          <textarea value={scheduleOpts.notes} onChange={(e) => setScheduleOpts(prev => ({ ...prev, notes: e.target.value }))} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-1.5 text-sm" rows={2} />
        </div>

        {/* Plate selector for multi-plate files */}
        {(() => {
          const plateIndices = payloadOptions?.plate_indices || []
          const summaries = plateData?.plates || plateIndices.map(i => ({ plate_index: i, is_sliced: true }))
          if (!summaries.length) return null
          return (
            <div className="mb-4">
              <label className="block text-xs text-[var(--brand-muted)] mb-1">Plate</label>
              <div className="flex flex-wrap gap-2">
                {summaries.map(plate => { const i = plate.plate_index; return (
                  <button
                    key={i}
                    type="button"
                    disabled={!plate.is_sliced || !plateIndices.includes(i)}
                    onClick={() => setScheduleOpts(prev => ({ ...prev, plate_index: i }))}
                    className={clsx('px-3 py-1 rounded text-xs border transition-colors',
                      scheduleOpts.plate_index === i
                        ? 'border-[var(--brand-primary)] bg-[var(--brand-primary)]/10 text-[var(--brand-primary)]'
                        : 'border-[var(--brand-card-border)] text-[var(--brand-muted)] hover:border-[var(--brand-card-border)]'
                    )}
                  >
                    Plate {i + 1} — {plate.is_sliced ? `${plate.print_time_seconds != null ? Math.round(plate.print_time_seconds / 60) + ' min' : 'Time unknown'} · ${plate.filament_grams != null ? plate.filament_grams + ' g' : 'Filament unknown'}` : 'Requires slicing'}
                  </button>
                )})}
              </div>
            </div>
          )
        })()}

        <p className="text-sm text-[var(--brand-muted)] mb-2">Assign to Printer</p>
        <div className="flex flex-wrap gap-2 mb-6">
          <button
            disabled={!!boundPrinter}
            onClick={() => setSelectedPrinter(null)}
            className={clsx('px-3 py-1.5 rounded-md border text-sm transition-colors',
              selectedPrinter === null ? 'border-[var(--brand-primary)] bg-[var(--brand-primary)]/10 text-[var(--brand-primary)]' : 'border-[var(--brand-card-border)] hover:border-[var(--brand-card-border)]'
            )}
          >
            Auto-assign
          </button>
          {sortedPrinters.map(p => {
            const compat = hasMatch(p)
            return (
              <button
                key={p.id}
                onClick={() => setSelectedPrinter(p.id)}
                className={clsx('px-3 py-1.5 rounded-md border text-sm transition-colors flex items-center gap-1.5',
                  selectedPrinter === p.id ? 'border-[var(--brand-primary)] bg-[var(--brand-primary)]/10 text-[var(--brand-primary)]' : 'border-[var(--brand-card-border)] hover:border-[var(--brand-card-border)]',
                )}
                title={compat ? 'Has matching variant' : variants.length > 0 ? 'No matching variant' : ''}
              >
                {compat && <span className="w-1.5 h-1.5 rounded-full bg-green-400" />}
                {p.name}
              </button>
            )
          })}
          {activePrinters.length === 0 && (
            <p className="text-xs text-[var(--brand-muted)] py-1">No active printers found</p>
          )}
        </div>

      {payloadError && <p role="alert" className="text-red-400 text-sm mb-3">Scheduling options could not be loaded: {payloadError.message}</p>}
      <div className="flex justify-end gap-3">
        <button onClick={onClose} disabled={isScheduling} className="px-4 py-2 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md text-sm transition-colors">Cancel</button>
        <button onClick={() => onConfirm(selectedPrinter, { ...scheduleOpts, quantity: Number(scheduleOpts.quantity), due_date: scheduleOpts.due_date ? scheduleOpts.due_date + 'T00:00:00Z' : null, notes: scheduleOpts.notes || null, print_file_id: payloadOptions?.print_file_id, payload_sha256: payloadOptions?.payload_sha256 })} disabled={isScheduling || loadingPayload || loadingPlates || !!payloadError || !!plateError || (!!payloadOptions?.plate_indices?.length && !payloadOptions.plate_indices.includes(scheduleOpts.plate_index)) || !Number.isInteger(Number(scheduleOpts.quantity)) || Number(scheduleOpts.quantity) < 1} className="px-4 py-2 bg-[var(--brand-primary)] hover:bg-[var(--brand-primary-hover)] rounded-md text-sm transition-colors">
          {isScheduling ? 'Scheduling...' : 'Create Job'}
        </button>
      </div>
    </Modal>
  )
}

export default function Models() {
  const org = useOrg()
  const queryClient = useQueryClient()
  const [showModal, setShowModal] = useState(false)
  const [editingModel, setEditingModel] = useState(null)
  const [scheduleModel, setScheduleModel] = useState(null)
  const [viewerModelId, setViewerModelId] = useState(null)
  const [viewerModelName, setViewerModelName] = useState('')
  const [searchParams, setSearchParams] = useSearchParams()
  const [categoryFilter, _setCategoryFilter] = useState(() => searchParams.get('category') || '')
  const [showFavoritesOnly, _setShowFavoritesOnly] = useState(() => searchParams.get('favorites') === 'true')
  const [revisionModel, setRevisionModel] = useState(null)
  const [searchQuery, _setSearchQuery] = useState(() => searchParams.get('q') || '')
  const [deleteConfirm, setDeleteConfirm] = useState(null)

  const updateSearchParams = useCallback((updates) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      Object.entries(updates).forEach(([key, value]) => {
        if (value && value !== '' && value !== 'false') {
          next.set(key, value)
        } else {
          next.delete(key)
        }
      })
      return next
    }, { replace: true })
  }, [setSearchParams])

  const setCategoryFilter = useCallback((value) => {
    _setCategoryFilter(value)
    updateSearchParams({ category: value })
  }, [updateSearchParams])

  const setShowFavoritesOnly = useCallback((value) => {
    _setShowFavoritesOnly(value)
    updateSearchParams({ favorites: value ? 'true' : '' })
  }, [updateSearchParams])

  const setSearchQuery = useCallback((value) => {
    _setSearchQuery(value)
    updateSearchParams({ q: value })
  }, [updateSearchParams])

  const { data: modelsData, isLoading } = useQuery({ queryKey: ['models', categoryFilter, org.orgId], queryFn: () => models.listWithPricing(categoryFilter || null, org.orgId) })
  // Viewers (students) submit library models for approval when the approval workflow is on;
  // the backend creates the job as 'submitted' for an operator/admin to approve.
  // Roles that cannot schedule directly (viewers by default RBAC) get the approval path.
  const mayScheduleDirectly = canDo('models.create')
  const { data: approvalSetting } = useQuery({ queryKey: ['approval-setting'], queryFn: () => getApprovalSetting(), enabled: !mayScheduleDirectly })
  const submitForApproval = !mayScheduleDirectly && !!approvalSetting?.require_job_approval

  // Auto-open schedule modal if ?schedule=modelId is present
  useEffect(() => {
    const scheduleId = searchParams.get('schedule')
    if (scheduleId && modelsData) {
      const model = modelsData.find(m => m.id === parseInt(scheduleId))
      if (model) {
        setScheduleModel(model)
        setSearchParams(prev => {
          const next = new URLSearchParams(prev)
          next.delete('schedule')
          return next
        }, { replace: true })
      }
    }
  }, [searchParams, modelsData, setSearchParams])
  const createModel = useMutation({
    mutationFn: models.create,
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['models'] }); toast.success('Model created') },
    onError: (err) => toast.error('Create model failed: ' + err.message),
  })
  const updateModel = useMutation({
    mutationFn: ({ id, data }) => models.update(id, data),
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['models'] }); toast.success('Model updated') },
    onError: (err) => toast.error('Update model failed: ' + err.message),
  })
  const deleteModel = useMutation({
    mutationFn: models.delete,
    onSuccess: () => { queryClient.invalidateQueries({ queryKey: ['models'] }); toast.success('Model deleted') },
    onError: (err) => toast.error('Delete model failed: ' + err.message),
  })
  const scheduleMutation = useMutation({
    mutationFn: ({ modelId, printerId, options }) => models.schedule(modelId, printerId, options),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
      setScheduleModel(null)
      toast.success(submitForApproval ? 'Job submitted for approval' : 'Print job scheduled')
    },
    onError: (err) => toast.error('Schedule failed: ' + err.message),
  })

  const handleSubmit = (data, modelId) => { if (modelId) { updateModel.mutate({ id: modelId, data }) } else { createModel.mutate(data) } }
  const handleEdit = (model) => { setEditingModel(model); setShowModal(true) }
  const handleDelete = (modelId) => {
    setDeleteConfirm(modelId)
  }
  const handleScheduleClick = (model) => { setScheduleModel(model) }
  const handleToggleFavorite = async (model) => {
    await updateModel.mutateAsync({ id: model.id, data: { is_favorite: !model.is_favorite } })
  }
  const handleScheduleConfirm = (printerId, options) => {
    if (scheduleModel) {
      scheduleMutation.mutate({ modelId: scheduleModel.id, printerId, options })
    }
  }

  const categories = [...new Set(modelsData?.map(m => m.category).filter(Boolean))]

  // Count models per category
  const categoryCounts = {}
  modelsData?.forEach(m => { if (m.category) categoryCounts[m.category] = (categoryCounts[m.category] || 0) + 1 })
  const favCount = modelsData?.filter(m => m.is_favorite).length || 0
  const libraryStatsAvailable = !!modelsData?.length && modelsData.every(m => m.job_statistics != null)
  const libraryHours = modelsData?.reduce((sum, m) => sum + (m.job_statistics?.actual_print_hours ?? 0), 0) ?? 0
  const libraryUnknown = modelsData?.reduce((sum, m) => sum + (m.job_statistics?.unknown_timing_job_count ?? 0), 0) ?? 0
  const mostPrinted = libraryStatsAvailable ? [...modelsData].filter(m => m.job_statistics.completed_job_count > 0)
    .sort((a, b) => b.job_statistics.completed_job_count - a.job_statistics.completed_job_count || a.id - b.id)[0] : null

  // Filter by search query
  const filteredModels = modelsData?.filter(m => {
    if (showFavoritesOnly && !m.is_favorite) return false
    if (searchQuery && ![m.name, m.default_filament_type].some(value =>
      value?.toLowerCase().includes(searchQuery.toLowerCase()))) return false
    return true
  })

  const statisticsAvailable = filteredModels?.every(m => m.job_statistics != null) ?? false
  const selectedStats = filteredModels?.reduce((sum, m) => ({
    jobs: sum.jobs + (m.job_statistics?.completed_job_count ?? 0),
    hours: sum.hours + (m.job_statistics?.actual_print_hours ?? 0),
    unknown: sum.unknown + (m.job_statistics?.unknown_timing_job_count ?? 0),
  }), { jobs: 0, hours: 0, unknown: 0 }) ?? { jobs: 0, hours: 0, unknown: 0 }

  return (
    <div className="p-4 md:p-6">
      <PageHeader icon={Box} title="Models" subtitle="Print model library">
        {canDo('models.create') && (
          <Button icon={Plus} onClick={() => { setEditingModel(null); setShowModal(true) }}>
            Add Model
          </Button>
        )}
      </PageHeader>

      {/* Search */}
      <SearchInput
        value={searchQuery}
        onChange={(e) => setSearchQuery(e.target.value)}
        placeholder="Search models..."
        className="max-w-md mb-4"
      />

      {categories.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-4 md:mb-6 items-center">
          <TabBar
            tabs={[
              { value: '', label: 'All', count: modelsData?.length || 0 },
              ...categories.map(cat => ({
                value: cat,
                label: cat,
                count: categoryCounts[cat] || 0,
              })),
            ]}
            active={categoryFilter}
            onChange={(val) => setCategoryFilter(val)}
          />
          <Button
            variant={showFavoritesOnly ? 'warning' : 'secondary'}
            size="sm"
            icon={Star}
            onClick={() => setShowFavoritesOnly(!showFavoritesOnly)}
          >
            Favorites ({favCount})
          </Button>
        </div>
      )}

      {/* Model Library Summary */}
      {modelsData?.length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2 md:gap-3 mb-6">
          <StatCard label="Models" value={modelsData.length} icon={Box} color="blue" />
          <StatCard label="Favorites" value={modelsData.filter(m => m.is_favorite).length} icon={Star} color="amber" />
          <StatCard label="Categories" value={categories.length} color="green" />
          <StatCard label="Costed" value={modelsData.filter(m => m.estimated_cost).length} color="purple" />
          <StatCard label="Total Print Hours" value={!libraryStatsAvailable ? 'Unavailable' : libraryUnknown && libraryHours === 0 ? 'Unknown' : `${libraryHours.toFixed(1)}h`} icon={Clock} color="blue" />
          <StatCard label="Most Printed" value={!libraryStatsAvailable ? 'Unavailable' : mostPrinted ? <span className="block break-words text-sm" title={mostPrinted.name}>{mostPrinted.name}</span> : 'None'} subtitle={mostPrinted ? `${mostPrinted.job_statistics.completed_job_count} completed jobs` : undefined} icon={PrinterIcon} color="green" />
        </div>
      )}
      {modelsData?.length > 0 && <p className="text-xs text-[var(--brand-muted)] mb-4">Totals for the current library view use measured time and completed generic jobs visible to you. Education jobs are excluded; most printed is ranked by completed job count.{libraryStatsAvailable && libraryUnknown > 0 ? ` Timing is unknown for ${libraryUnknown} completed jobs.` : ''}</p>}

      {modelsData?.length > 0 && (
        <div className="mb-6" aria-label="Selected model job statistics">
          <p className="text-xs text-[var(--brand-muted)] mb-2">Generic visible jobs for selected models; Education jobs excluded.</p>
          <div className="grid grid-cols-2 gap-2 md:gap-3">
            <StatCard label="Completed jobs" value={statisticsAvailable ? selectedStats.jobs : 'Unavailable'} icon={PrinterIcon} color="green" />
            <StatCard label="Actual print hours" value={!statisticsAvailable ? 'Unavailable' : selectedStats.unknown > 0 && selectedStats.hours === 0 ? 'Unknown' : `${selectedStats.hours.toFixed(1)}h`} icon={Clock} color="blue" />
          </div>
          {statisticsAvailable && selectedStats.unknown > 0 && (
            <p className="text-xs text-[var(--brand-muted)] mt-2">Timing unknown for {selectedStats.unknown} completed {selectedStats.unknown === 1 ? 'job' : 'jobs'}; hours include measured time only.</p>
          )}
        </div>
      )}

      {isLoading ? (
        <div className="text-center py-12 text-[var(--brand-muted)] text-sm">Loading models...</div>
      ) : modelsData?.length === 0 ? (
        <EmptyState
          icon={Upload}
          title="No models defined yet"
          description="Upload a .3mf file or add models manually to get started."
        >
          <Link to="/upload">
            <Button variant="success">Upload .3mf</Button>
          </Link>
          <Button onClick={() => setShowModal(true)}>Add Manually</Button>
        </EmptyState>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3 md:gap-4">
          {filteredModels?.map((model) => (
            <ModelCard key={model.id} model={model} onEdit={handleEdit} onDelete={handleDelete} onSchedule={handleScheduleClick} onToggleFavorite={handleToggleFavorite} onView3D={(m) => { setViewerModelId(m.id); setViewerModelName(m.name) }} onRevisions={(m) => setRevisionModel(m)} submitForApproval={submitForApproval} />
          ))}
        </div>
      )}

      <ModelModal isOpen={showModal} onClose={() => { setShowModal(false); setEditingModel(null) }} onSubmit={handleSubmit} editingModel={editingModel} />

      <ScheduleModal
        isOpen={!!scheduleModel}
        onClose={() => setScheduleModel(null)}
        model={scheduleModel}
        onConfirm={handleScheduleConfirm}
        isScheduling={scheduleMutation.isPending}
      />

      {viewerModelId && (
        <Suspense fallback={<div className="flex items-center justify-center p-8 text-[var(--brand-muted)]">Loading 3D viewer...</div>}>
          <ModelViewer
            modelId={viewerModelId}
            modelName={viewerModelName}
            onClose={() => { setViewerModelId(null); setViewerModelName('') }}
          />
        </Suspense>
      )}

      {revisionModel && (
        <ModelRevisionPanel
          modelId={revisionModel.id}
          modelName={revisionModel.name}
          onClose={() => setRevisionModel(null)}
        />
      )}

      <ConfirmModal
        open={!!deleteConfirm}
        onConfirm={() => { deleteModel.mutate(deleteConfirm); setDeleteConfirm(null) }}
        onCancel={() => setDeleteConfirm(null)}
        title="Delete Model"
        message="Permanently delete this model and its variants? Existing jobs using this model will not be affected, but the model data will be lost."
        confirmText="Delete Model"
        confirmVariant="danger"
      />
    </div>
  )
}
