import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Power, PowerOff, Palette, X, Settings, Search } from 'lucide-react'
import clsx from 'clsx'
import { printers, filaments } from '../api'

function FilamentSlotEditor({ slot, allFilaments, onSave }) {
  const [isEditing, setIsEditing] = useState(false)
  const [search, setSearch] = useState('')
  const [selectedFilament, setSelectedFilament] = useState(null)

  const filteredFilaments = allFilaments?.filter(f => 
    f.display_name.toLowerCase().includes(search.toLowerCase()) ||
    f.brand.toLowerCase().includes(search.toLowerCase()) ||
    f.name.toLowerCase().includes(search.toLowerCase())
  ) || []

  // Group by source (spoolman first, then by brand)
  const spoolmanFilaments = filteredFilaments.filter(f => f.source === 'spoolman')
  const libraryFilaments = filteredFilaments.filter(f => f.source === 'library')

  const handleSelect = (filament) => {
    setSelectedFilament(filament)
    onSave({ 
      color: `${filament.brand} ${filament.name}`,
      color_hex: filament.color_hex,
      filament_type: filament.material,
      spoolman_spool_id: filament.source === 'spoolman' ? parseInt(filament.id.replace('spool_', '')) : null
    })
    setIsEditing(false)
    setSearch('')
  }

  const handleClear = () => {
    onSave({ color: null, color_hex: null, spoolman_spool_id: null })
    setIsEditing(false)
  }

  const colorHex = slot.color_hex

  if (isEditing) {
    return (
      <div className="bg-farm-700 rounded-lg p-2 min-w-[200px]">
        <div className="text-xs text-farm-400 mb-1">Slot {slot.slot_number}</div>
        <div className="relative mb-2">
          <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-farm-500" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search filaments..."
            className="w-full bg-farm-800 border border-farm-600 rounded pl-7 pr-2 py-1 text-sm"
            autoFocus
          />
        </div>
        <div className="max-h-48 overflow-y-auto space-y-1">
          {spoolmanFilaments.length > 0 && (
            <>
              <div className="text-xs text-print-400 font-medium px-1">From Spoolman</div>
              {spoolmanFilaments.slice(0, 10).map(f => (
                <button
                  key={f.id}
                  onClick={() => handleSelect(f)}
                  className="w-full flex items-center gap-2 px-2 py-1 hover:bg-farm-600 rounded text-left text-sm"
                >
                  <div 
                    className="w-4 h-4 rounded border border-farm-500" 
                    style={{ backgroundColor: f.color_hex ? `#${f.color_hex}` : '#666' }}
                  />
                  <span className="truncate flex-1">{f.name}</span>
                  {f.remaining_weight && (
                    <span className="text-xs text-farm-400">{Math.round(f.remaining_weight)}g</span>
                  )}
                </button>
              ))}
            </>
          )}
          {libraryFilaments.length > 0 && (
            <>
              <div className="text-xs text-farm-400 font-medium px-1 mt-2">From Library</div>
              {libraryFilaments.slice(0, 20).map(f => (
                <button
                  key={f.id}
                  onClick={() => handleSelect(f)}
                  className="w-full flex items-center gap-2 px-2 py-1 hover:bg-farm-600 rounded text-left text-sm"
                >
                  <div 
                    className="w-4 h-4 rounded border border-farm-500" 
                    style={{ backgroundColor: f.color_hex ? `#${f.color_hex}` : '#666' }}
                  />
                  <span className="truncate">{f.display_name}</span>
                </button>
              ))}
            </>
          )}
          {filteredFilaments.length === 0 && (
            <div className="text-xs text-farm-500 px-1 py-2">No filaments found</div>
          )}
        </div>
        <div className="flex gap-1 mt-2">
          <button onClick={handleClear} className="flex-1 text-xs bg-farm-600 hover:bg-farm-500 rounded py-1">Clear</button>
          <button onClick={() => { setIsEditing(false); setSearch('') }} className="flex-1 text-xs bg-farm-600 hover:bg-farm-500 rounded py-1">Cancel</button>
        </div>
      </div>
    )
  }

  return (
    <div className="bg-farm-800 rounded-lg p-2 cursor-pointer hover:bg-farm-700 transition-colors" onClick={() => setIsEditing(true)}>
      <div className="text-xs text-farm-500 mb-1">Slot {slot.slot_number}</div>
      <div className="flex items-center gap-2">
        {colorHex && (
          <div 
            className="w-3 h-3 rounded-full border border-farm-600 flex-shrink-0" 
            style={{ backgroundColor: `#${colorHex}` }}
          />
        )}
        <div className="text-sm font-medium truncate">{slot.color || 'Empty'}</div>
      </div>
      <div className="text-xs text-farm-500">{slot.filament_type}</div>
    </div>
  )
}

function PrinterCard({ printer, allFilaments, onDelete, onToggleActive, onUpdateSlot, onEdit }) {
  return (
    <div className="bg-farm-900 rounded-xl border border-farm-800 overflow-hidden h-fit">
      <div className="p-4 border-b border-farm-800 flex items-center justify-between">
        <div>
          <h3 className="font-display font-semibold text-lg">{printer.name}</h3>
          <p className="text-sm text-farm-500">{printer.model || 'Unknown model'}</p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => onEdit(printer)} className="p-2 text-farm-400 hover:bg-farm-800 rounded-lg transition-colors" title="Edit Printer Settings">
            <Settings size={18} />
          </button>
          <button onClick={() => onToggleActive(printer.id, !printer.is_active)} className={clsx('p-2 rounded-lg transition-colors', printer.is_active ? 'text-print-400 hover:bg-print-900/50' : 'text-farm-500 hover:bg-farm-800')} title={printer.is_active ? 'Deactivate' : 'Activate'}>
            {printer.is_active ? <Power size={18} /> : <PowerOff size={18} />}
          </button>
          <button onClick={() => onDelete(printer.id)} className="p-2 text-farm-500 hover:text-red-400 hover:bg-red-900/50 rounded-lg transition-colors" title="Delete">
            <Trash2 size={18} />
          </button>
        </div>
      </div>
      <div className="p-4">
        <div className="flex items-center gap-2 mb-3">
          <Palette size={16} className="text-farm-500" />
          <span className="text-sm text-farm-400">Loaded Filaments</span>
          <span className="text-xs text-farm-600">(click to edit)</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '8px' }}>
          {printer.filament_slots?.map((slot) => (
            <FilamentSlotEditor 
              key={slot.id} 
              slot={slot} 
              allFilaments={allFilaments}
              onSave={(data) => onUpdateSlot(printer.id, slot.slot_number, data)} 
            />
          ))}
        </div>
      </div>
      <div className="px-4 py-3 bg-farm-950 border-t border-farm-800">
        <div className="flex items-center justify-between text-sm">
          <span className="text-farm-500">Status</span>
          <span className={printer.is_active ? 'text-print-400' : 'text-farm-500'}>{printer.is_active ? 'Active' : 'Inactive'}</span>
        </div>
      </div>
    </div>
  )
}

function PrinterModal({ isOpen, onClose, onSubmit, printer }) {
  const [formData, setFormData] = useState({ name: '', model: '', slot_count: 4 })

  useEffect(() => {
    if (printer) {
      setFormData({ 
        name: printer.name || '', 
        model: printer.model || '', 
        slot_count: printer.slot_count || 4 
      })
    } else {
      setFormData({ name: '', model: '', slot_count: 4 })
    }
  }, [printer])

  if (!isOpen) return null

  const isEditing = !!printer

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-farm-900 rounded-xl w-full max-w-md p-6 border border-farm-700">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-display font-semibold">{isEditing ? 'Edit Printer' : 'Add New Printer'}</h2>
          <button onClick={onClose} className="text-farm-500 hover:text-farm-300"><X size={20} /></button>
        </div>
        <form onSubmit={(e) => { e.preventDefault(); onSubmit(formData, printer?.id) }} className="space-y-4">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Printer Name {!isEditing && '*'}</label>
            <input type="text" required={!isEditing} value={formData.name} onChange={(e) => setFormData(prev => ({ ...prev, name: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2" placeholder="e.g., X1C, P1S-01" />
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Model</label>
            <input type="text" value={formData.model} onChange={(e) => setFormData(prev => ({ ...prev, model: e.target.value }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2" placeholder="e.g., Bambu Lab X1 Carbon" />
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Filament Slots</label>
            <select value={formData.slot_count} onChange={(e) => setFormData(prev => ({ ...prev, slot_count: Number(e.target.value) }))} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2">
              <option value={1}>1 slot (no AMS)</option>
              <option value={4}>4 slots (1x AMS)</option>
              <option value={5}>5 slots (AMS + HT slot)</option>
              <option value={8}>8 slots (2x AMS)</option>
              <option value={9}>9 slots (2x AMS + HT slot)</option>
              <option value={12}>12 slots (3x AMS)</option>
              <option value={16}>16 slots (4x AMS)</option>
            </select>
            {isEditing && formData.slot_count !== printer.slot_count && (
              <p className="text-xs text-amber-400 mt-1">Note: Changing slot count will reset filament colors</p>
            )}
          </div>
          <div className="flex justify-end gap-3 pt-4">
            <button type="button" onClick={onClose} className="px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg transition-colors">Cancel</button>
            <button type="submit" className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors">{isEditing ? 'Save Changes' : 'Add Printer'}</button>
          </div>
        </form>
      </div>
    </div>
  )
}

export default function Printers() {
  const queryClient = useQueryClient()
  const [showModal, setShowModal] = useState(false)
  const [editingPrinter, setEditingPrinter] = useState(null)

  const { data: printersData, isLoading } = useQuery({ queryKey: ['printers'], queryFn: () => printers.list() })
  const { data: filamentsData } = useQuery({ queryKey: ['filaments-combined'], queryFn: () => filaments.combined() })
  
  const createPrinter = useMutation({ mutationFn: printers.create, onSuccess: () => { queryClient.invalidateQueries(['printers']); setShowModal(false) } })
  const updatePrinter = useMutation({ mutationFn: ({ id, data }) => printers.update(id, data), onSuccess: () => { queryClient.invalidateQueries(['printers']); setShowModal(false); setEditingPrinter(null) } })
  const deletePrinter = useMutation({ mutationFn: printers.delete, onSuccess: () => queryClient.invalidateQueries(['printers']) })
  const updateSlot = useMutation({ mutationFn: ({ printerId, slotNumber, data }) => printers.updateSlot(printerId, slotNumber, data), onSuccess: () => queryClient.invalidateQueries(['printers']) })

  const handleSubmit = (data, printerId) => {
    if (printerId) {
      updatePrinter.mutate({ id: printerId, data })
    } else {
      createPrinter.mutate(data)
    }
  }

  const handleEdit = (printer) => {
    setEditingPrinter(printer)
    setShowModal(true)
  }

  const handleCloseModal = () => {
    setShowModal(false)
    setEditingPrinter(null)
  }

  return (
    <div className="p-8">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-display font-bold">Printers</h1>
          <p className="text-farm-500 mt-1">Manage your print farm</p>
        </div>
        <button onClick={() => setShowModal(true)} className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors">
          <Plus size={18} /> Add Printer
        </button>
      </div>
      {isLoading ? (
        <div className="text-center py-12 text-farm-500">Loading printers...</div>
      ) : printersData?.length === 0 ? (
        <div className="bg-farm-900 rounded-xl border border-farm-800 p-12 text-center">
          <p className="text-farm-500 mb-4">No printers configured yet.</p>
          <button onClick={() => setShowModal(true)} className="px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg transition-colors">Add Your First Printer</button>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-6 items-start">
          {printersData?.map((printer) => (
            <PrinterCard 
              key={printer.id} 
              printer={printer} 
              allFilaments={filamentsData}
              onDelete={(id) => { if (confirm('Delete this printer?')) deletePrinter.mutate(id) }} 
              onToggleActive={(id, active) => updatePrinter.mutate({ id, data: { is_active: active } })} 
              onUpdateSlot={(pid, slot, data) => updateSlot.mutate({ printerId: pid, slotNumber: slot, data })} 
              onEdit={handleEdit} 
            />
          ))}
        </div>
      )}
      <PrinterModal isOpen={showModal} onClose={handleCloseModal} onSubmit={handleSubmit} printer={editingPrinter} />
    </div>
  )
}
