import { useState, useEffect, useRef } from 'react'
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
  Printer as PrinterIcon
} from 'lucide-react'
import clsx from 'clsx'

import { models, filaments, printers } from '../api'
import { canDo } from '../permissions'

function ModelCard({ model, onEdit, onDelete, onSchedule }) {
  return (
    <div className="bg-farm-900 rounded-xl border border-farm-800 overflow-hidden hover:border-farm-700 transition-colors">
      <div className="h-28 md:h-32 bg-farm-950 flex items-center justify-center">
        {model.thumbnail_b64 ? (
          <img 
            src={`data:image/png;base64,${model.thumbnail_b64}`}
            alt={model.name}
            className="h-full w-full object-contain p-1"
          />
        ) : model.thumbnail_url ? (
          <img 
            src={model.thumbnail_url} 
            alt={model.name}
            className="h-full w-full object-cover"
          />
        ) : (
          <div className="text-4xl text-farm-700">📦</div>
        )}
      </div>
      <div className="p-3 md:p-4">
        <div className="flex items-start justify-between mb-2 gap-1">
          <div className="min-w-0">
            <h3 className="font-medium text-sm md:text-base truncate">{model.name}</h3>
            <div className="flex items-center gap-2 mt-0.5">
              {model.default_filament_type && (
                <span className="text-xs px-1.5 py-0.5 bg-farm-800 text-farm-400 rounded">{model.default_filament_type}</span>
              )}
              {model.variant_count > 1 && (
                <span className="text-xs px-1.5 py-0.5 bg-blue-500/20 text-blue-300 rounded">{model.variant_count} variants</span>
              )}
            </div>
          </div>
          
          <div className="flex items-center gap-0.5 flex-shrink-0">
            {canDo('models.create') && <button
              onClick={() => onSchedule(model)}
              className="p-1 md:p-1.5 text-print-400 hover:bg-print-900/50 rounded transition-colors"
              title="Schedule print job"
            >
              <CalendarPlus size={14} />
            </button>}
            {canDo('models.edit') && <button
              onClick={() => onEdit(model)}
              className="p-1 md:p-1.5 text-farm-400 hover:bg-farm-800 rounded transition-colors"
            >
              <Pencil size={14} />
            </button>}
            {canDo('models.delete') && <button
              onClick={() => onDelete(model.id)}
              className="p-1 md:p-1.5 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded transition-colors"
            >
              <Trash2 size={14} />
            </button>}
          </div>
        </div>
        
        <div className="flex items-center gap-3 md:gap-4 text-xs md:text-sm text-farm-400 mt-3">
          {model.build_time_hours > 0 && (
            <div className="flex items-center gap-1">
              <Clock size={12} />
              <span>{model.build_time_hours < 1 ? `${Math.round(model.build_time_hours * 60)}m` : `${model.build_time_hours.toFixed(1)}h`}</span>
            </div>
          )}
          {model.total_filament_grams > 0 && (
            <div className="flex items-center gap-1">
              <Scale size={12} />
              <span>{model.total_filament_grams.toFixed(0)}g</span>
            </div>
          )}
        </div>
        
        {model.required_colors?.length > 0 && (
          <div className="flex flex-wrap gap-1 mt-3">
            {model.required_colors.map((color, i) => (
              <div 
                key={i}
                className="w-5 h-5 rounded-full border border-farm-600"
                style={{ backgroundColor: color }}
                title={color}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function FilamentSelector({ value, onChange, filamentData, placeholder }) {
  const [isOpen, setIsOpen] = useState(false)
  const [search, setSearch] = useState('')
  const [displayValue, setDisplayValue] = useState(value || '')
  const containerRef = useRef(null)

  useEffect(() => { setDisplayValue(value || '') }, [value])

  useEffect(() => {
    const handleClickOutside = (e) => {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setIsOpen(false)
        setSearch('')
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  const filterText = isOpen ? search : ''
  const filteredFilaments = (filamentData || []).filter(f => {
    if (!filterText) return true
    return f.display_name?.toLowerCase().includes(filterText.toLowerCase()) ||
      f.name?.toLowerCase().includes(filterText.toLowerCase()) ||
      f.brand?.toLowerCase().includes(filterText.toLowerCase())
  })

  const spoolmanFilaments = filteredFilaments.filter(f => f.source === 'spoolman')
  const libraryFilaments = filteredFilaments.filter(f => f.source === 'library')

  const handleSelect = (filament) => {
    const colorName = filament.display_name || `${filament.brand} ${filament.name} (${filament.material})`
    onChange(colorName)
    setDisplayValue(colorName)
    setSearch('')
    setIsOpen(false)
  }

  const handleInputChange = (e) => {
    const val = e.target.value
    setSearch(val)
    setDisplayValue(val)
    onChange(val)
  }

  const handleFocus = () => { setIsOpen(true); setSearch('') }
  const handleBlur = () => { setTimeout(() => { setIsOpen(false); setSearch('') }, 150) }

  return (
    <div className="relative" ref={containerRef}>
      <div className="relative">
        <input
          type="text"
          value={isOpen ? search : displayValue}
          onChange={handleInputChange}
          onFocus={handleFocus}
          onBlur={handleBlur}
          className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm pr-8"
          placeholder={isOpen ? "Type to search..." : placeholder}
        />
        <button
          type="button"
          onMouseDown={(e) => {
            e.preventDefault()
            if (isOpen) { setIsOpen(false); setSearch('') } else { setIsOpen(true); setSearch('') }
          }}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-farm-500 hover:text-farm-300"
        >
          <ChevronDown size={16} className={clsx(isOpen && "rotate-180 transition-transform")} />
        </button>
      </div>
      
      {isOpen && (spoolmanFilaments.length > 0 || libraryFilaments.length > 0) && (
        <div className="absolute z-50 mt-1 w-full max-h-56 overflow-auto bg-farm-800 border border-farm-700 rounded-lg shadow-lg">
          {spoolmanFilaments.length > 0 && (
            <>
              <div className="text-xs text-print-400 font-medium px-3 py-1 bg-farm-900 sticky top-0">From Spoolman</div>
              {spoolmanFilaments.slice(0, 15).map(f => (
                <div key={f.id} onMouseDown={(e) => { e.preventDefault(); handleSelect(f) }} className="flex items-center gap-2 px-3 py-1.5 hover:bg-farm-700 cursor-pointer">
                  <div className="w-4 h-4 rounded border border-farm-500 flex-shrink-0" style={{ backgroundColor: f.color_hex ? `#${f.color_hex}` : '#666' }} />
                  <span className="text-sm truncate flex-1">{f.name}</span>
                  {f.remaining_weight && <span className="text-xs text-farm-400">{Math.round(f.remaining_weight)}g</span>}
                </div>
              ))}
            </>
          )}
          {libraryFilaments.length > 0 && (
            <>
              <div className="text-xs text-farm-400 font-medium px-3 py-1 bg-farm-900 sticky top-0">From Library</div>
              {libraryFilaments.slice(0, 20).map(f => (
                <div key={f.id} onMouseDown={(e) => { e.preventDefault(); handleSelect(f) }} className="flex items-center gap-2 px-3 py-1.5 hover:bg-farm-700 cursor-pointer">
                  <div className="w-4 h-4 rounded border border-farm-500 flex-shrink-0" style={{ backgroundColor: f.color_hex ? `#${f.color_hex}` : '#666' }} />
                  <span className="text-sm truncate">{f.display_name}</span>
                </div>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function ColorSlotInput({ index, color, grams, onChange, onRemove, filamentData }) {
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1">
        <FilamentSelector value={color} onChange={(val) => onChange(index, 'color', val)} filamentData={filamentData} placeholder={`Color ${index + 1}`} />
      </div>
      <div className="w-20 md:w-24">
        <input type="number" min="0" step="0.1" value={grams} onChange={(e) => onChange(index, 'grams', e.target.value)} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-2 md:px-3 py-2 text-sm" placeholder="g" />
      </div>
      <button type="button" onClick={() => onRemove(index)} className="p-2 text-farm-500 hover:text-red-400 hover:bg-red-900/30 rounded transition-colors">
        <X size={16} />
      </button>
    </div>
  )
}

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
  const addColorSlot = () => { if (colorSlots.length < 4) setColorSlots(prev => [...prev, { color: '', grams: '' }]) }
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

  if (!isOpen) return null

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4">
      <div className="bg-farm-900 rounded-t-xl sm:rounded-xl w-full max-w-lg max-h-[90vh] overflow-y-auto p-4 sm:p-6 border border-farm-700">
        <h2 className="text-lg sm:text-xl font-display font-semibold mb-4">{editingModel ? 'Edit Model' : 'Add New Model'}</h2>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Model Name *</label>
            <input type="text" required value={formData.name} onChange={(e) => setFormData(prev => ({ ...prev, name: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="e.g., Crocodile (Mini Critter)" />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-farm-400 mb-1">Category</label>
              <input type="text" value={formData.category} onChange={(e) => setFormData(prev => ({ ...prev, category: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="e.g., Mini Critters" />
            </div>
            <div>
              <label className="block text-sm text-farm-400 mb-1">Build Time (hours)</label>
              <input type="number" step="any" min="0" value={formData.build_time_hours} onChange={(e) => setFormData(prev => ({ ...prev, build_time_hours: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="e.g., 15.5" />
            </div>
          </div>
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="block text-sm text-farm-400">Filament Colors & Usage</label>
              {colorSlots.length < 4 && (
                <button type="button" onClick={addColorSlot} className="text-xs text-print-400 hover:text-print-300 flex items-center gap-1">
                  <Plus size={14} /> Add Color
                </button>
              )}
            </div>
            <div className="space-y-2">
              {colorSlots.map((slot, i) => (
                <ColorSlotInput key={i} index={i} color={slot.color} grams={slot.grams} onChange={handleColorChange} onRemove={removeColorSlot} filamentData={filamentData} />
              ))}
            </div>
            {totalFilament > 0 && (
              <div className="mt-2 text-sm text-farm-400 flex items-center gap-2">
                <Scale size={14} /> Total: <span className="text-white font-medium">{totalFilament.toFixed(1)}g</span>
              </div>
            )}
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-farm-400 mb-1">Cost per Item ($)</label>
              <input type="number" step="0.01" min="0" value={formData.cost_per_item} onChange={(e) => setFormData(prev => ({ ...prev, cost_per_item: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" placeholder="e.g., 5.50" />
            </div>
            <div>
              <label className="block text-sm text-farm-400 mb-1"># on Bed</label>
              <input type="number" min="1" value={formData.units_per_bed} onChange={(e) => setFormData(prev => ({ ...prev, units_per_bed: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
            </div>
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Markup %</label>
            <input type="number" min="0" value={formData.markup_percent} onChange={(e) => setFormData(prev => ({ ...prev, markup_percent: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Notes</label>
            <textarea value={formData.notes} onChange={(e) => setFormData(prev => ({ ...prev, notes: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 h-20 text-sm" placeholder="Optional notes..." />
          </div>
          <div className="flex justify-end gap-3 pt-4">
            <button type="button" onClick={onClose} className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors text-sm">Cancel</button>
            <button type="submit" className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm">{editingModel ? 'Save Changes' : 'Add Model'}</button>
          </div>
        </form>
      </div>
    </div>
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
  const [selectedPrinter, setSelectedPrinter] = useState(null)

  useEffect(() => { if (isOpen) setSelectedPrinter(null) }, [isOpen])

  const activePrinters = printersData?.filter(p => p.is_active) || []
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
  
  if (!isOpen || !model) return null
  
  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4">
      <div className="bg-farm-900 rounded-t-xl sm:rounded-xl w-full max-w-md p-4 sm:p-6 border border-farm-700">
        <h2 className="text-lg font-display font-semibold mb-1">Schedule Print</h2>
        <p className="text-sm text-farm-500 mb-2">{model.name}</p>
        
        {variants.length > 0 && (
          <div className="flex flex-wrap gap-1 mb-3">
            {variants.map(v => (
              <span key={v.id} className="px-2 py-0.5 bg-blue-500/20 text-blue-300 rounded text-xs">{v.printer_model}</span>
            ))}
          </div>
        )}
        
        {model.thumbnail_b64 && (
          <div className="h-32 bg-farm-950 rounded-lg mb-4 flex items-center justify-center overflow-hidden">
            <img src={`data:image/png;base64,${model.thumbnail_b64}`} alt={model.name} className="h-full object-contain" />
          </div>
        )}
        
        <div className="flex items-center gap-4 text-sm text-farm-400 mb-4">
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
        
        <p className="text-sm text-farm-400 mb-2">Assign to Printer</p>
        <div className="flex flex-wrap gap-2 mb-6">
          <button 
            onClick={() => setSelectedPrinter(null)} 
            className={clsx('px-3 py-1.5 rounded-lg border text-sm transition-colors', 
              selectedPrinter === null ? 'border-print-500 bg-print-900/30 text-print-400' : 'border-farm-700 hover:border-farm-500'
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
                className={clsx('px-3 py-1.5 rounded-lg border text-sm transition-colors flex items-center gap-1.5', 
                  selectedPrinter === p.id ? 'border-print-500 bg-print-900/30 text-print-400' : 'border-farm-700 hover:border-farm-500',
                )}
                title={compat ? 'Has matching variant' : variants.length > 0 ? 'No matching variant' : ''}
              >
                {compat && <span className="w-1.5 h-1.5 rounded-full bg-green-400" />}
                {p.name}
              </button>
            )
          })}
          {activePrinters.length === 0 && (
            <p className="text-xs text-farm-500 py-1">No active printers found</p>
          )}
        </div>
        
        <div className="flex justify-end gap-3">
          <button onClick={onClose} disabled={isScheduling} className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg text-sm transition-colors">Cancel</button>
          <button onClick={() => onConfirm(selectedPrinter)} disabled={isScheduling} className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-sm transition-colors">
            {isScheduling ? 'Scheduling...' : 'Create Job'}
          </button>
        </div>
      </div>
    </div>
  )
}

export default function Models() {
  const queryClient = useQueryClient()
  const [showModal, setShowModal] = useState(false)
  const [editingModel, setEditingModel] = useState(null)
  const [scheduleModel, setScheduleModel] = useState(null)
  const [selectedPrinter, setSelectedPrinter] = useState('')
  const [categoryFilter, setCategoryFilter] = useState('')

  const { data: modelsData, isLoading } = useQuery({ queryKey: ['models', categoryFilter], queryFn: () => models.list(categoryFilter || null) })
  const createModel = useMutation({ mutationFn: models.create, onSuccess: () => queryClient.invalidateQueries(['models']) })
  const updateModel = useMutation({ mutationFn: ({ id, data }) => models.update(id, data), onSuccess: () => queryClient.invalidateQueries(['models']) })
  const deleteModel = useMutation({ mutationFn: models.delete, onSuccess: () => queryClient.invalidateQueries(['models']) })
  const scheduleMutation = useMutation({ 
    mutationFn: ({ modelId, printerId }) => models.schedule(modelId, printerId), 
    onSuccess: () => { 
      queryClient.invalidateQueries(['jobs'])
      setScheduleModel(null) 
    } 
  })

  const handleSubmit = (data, modelId) => { if (modelId) { updateModel.mutate({ id: modelId, data }) } else { createModel.mutate(data) } }
  const handleEdit = (model) => { setEditingModel(model); setShowModal(true) }
  const handleDelete = (modelId) => { if (confirm('Delete this model? Jobs using this model will not be affected.')) deleteModel.mutate(modelId) }
  const handleScheduleClick = (model) => { setScheduleModel(model) }
  const handleScheduleConfirm = (printerId) => {
    if (scheduleModel) {
      scheduleMutation.mutate({ modelId: scheduleModel.id, printerId })
    }
  }

  const categories = [...new Set(modelsData?.map(m => m.category).filter(Boolean))]

  return (
    <div className="p-4 md:p-8">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4 md:mb-6">
        <div>
          <h1 className="text-2xl md:text-3xl font-display font-bold">Models</h1>
          <p className="text-farm-500 text-sm mt-1">Print model library</p>
        </div>
        {canDo('models.create') && <button onClick={() => { setEditingModel(null); setShowModal(true) }} className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm self-start">
          <Plus size={16} /> Add Model
        </button>}
      </div>

      {categories.length > 0 && (
        <div className="flex gap-2 mb-4 md:mb-6 flex-wrap">
          <button onClick={() => setCategoryFilter('')} className={clsx('px-3 py-1.5 rounded-lg text-sm transition-colors', !categoryFilter ? 'bg-print-600 text-white' : 'bg-farm-800 text-farm-400 hover:bg-farm-700')}>All</button>
          {categories.map((cat) => (
            <button key={cat} onClick={() => setCategoryFilter(cat)} className={clsx('px-3 py-1.5 rounded-lg text-sm transition-colors', categoryFilter === cat ? 'bg-print-600 text-white' : 'bg-farm-800 text-farm-400 hover:bg-farm-700')}>{cat}</button>
          ))}
        </div>
      )}

      {isLoading ? (
        <div className="text-center py-12 text-farm-500 text-sm">Loading models...</div>
      ) : modelsData?.length === 0 ? (
        <div className="bg-farm-900 rounded-xl border border-farm-800 p-8 md:p-12 text-center">
          <p className="text-farm-500 mb-4">No models defined yet.</p>
          <p className="text-sm text-farm-600 mb-4">Upload a .3mf file or add models manually to get started.</p>
          <button onClick={() => setShowModal(true)} className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors text-sm">Add Your First Model</button>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3 md:gap-4">
          {modelsData?.map((model) => (
            <ModelCard key={model.id} model={model} onEdit={handleEdit} onDelete={handleDelete} onSchedule={handleScheduleClick} />
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
    </div>
  )
}
