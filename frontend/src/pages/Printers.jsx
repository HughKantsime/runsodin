import { useState, useEffect, useRef } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Power, PowerOff, Palette, X, Settings, Search, GripVertical, RefreshCw, AlertTriangle } from 'lucide-react'
import clsx from 'clsx'
import { printers, filaments } from '../api'

// Helper for direct API calls that aren't in api.js yet
const API_KEY = '5464389e808f206efd9f9febef7743ff7a16911797cb0f058e805c82b33396ce'
const apiHeaders = {
  'Content-Type': 'application/json',
  'X-API-Key': API_KEY
}

function FilamentSlotEditor({ slot, allFilaments, spools, printerId, onSave }) {
  const [isEditing, setIsEditing] = useState(false)
  const [search, setSearch] = useState('')
  const filteredFilaments = allFilaments?.filter(f => 
    f.display_name.toLowerCase().includes(search.toLowerCase()) ||
    f.brand.toLowerCase().includes(search.toLowerCase()) ||
    f.name.toLowerCase().includes(search.toLowerCase())
  ) || []

  // Group by source (spoolman first, then by brand)
  const spoolmanFilaments = filteredFilaments.filter(f => f.source === 'spoolman')
  const libraryFilaments = filteredFilaments.filter(f => f.source === 'library')
  
  // Filter tracked spools
  const filteredSpools = (spools?.filter(s => 
    s.filament_brand?.toLowerCase().includes(search.toLowerCase()) ||
    s.filament_name?.toLowerCase().includes(search.toLowerCase())
  ).sort((a, b) => {
    if (a.id === slot.assigned_spool_id) return -1;
    if (b.id === slot.assigned_spool_id) return 1;
    return 0;
  })) || []
  
  const handleSelectSpool = async (spool) => {
    // Assign spool to slot via API
    await fetch(`/api/printers/${printerId}/slots/${slot.slot_number}/assign?spool_id=${spool.id}`, {
      method: "POST",
      headers: { "X-API-Key": API_KEY }
    })
    onSave({
      color: `${spool.filament_brand} ${spool.filament_name}`,
      color_hex: spool.filament_color_hex,
      filament_type: spool.filament_material,
      assigned_spool_id: spool.id
    })
    setIsEditing(false)
    setSearch("")
  }

  const handleSelect = (filament) => {
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

  // Shorten long filament names
  const getShortName = (color) => {
    if (!color) return 'Empty'
    // Remove brand prefix like "Bambu Lab"
    const brands = ['Bambu Lab', 'Polymaker', 'Hatchbox', 'eSun', 'Prusament', 'Overture', 'Generic']
    let short = color
    for (const brand of brands) {
      if (color.startsWith(brand + ' ')) {
        short = color.slice(brand.length + 1)
        break
      }
    }
    // Also truncate hex codes
    if (short.length > 12) {
      return short.slice(0, 10) + '...'
    }
    return short
  }

  return (
    <>
      <div 
        className="bg-farm-800 rounded-lg p-2 cursor-pointer hover:bg-farm-700 transition-colors min-w-0 overflow-hidden" 
        onClick={() => setIsEditing(true)}
      >
        <div className="text-xs text-farm-500 mb-1">Slot {slot.slot_number}</div>
        <div className="flex items-center gap-2 min-w-0">
          <div 
            className="w-3 h-3 rounded-full border border-farm-600 flex-shrink-0" 
            style={{ backgroundColor: colorHex ? `#${colorHex}` : '#333' }}
          />
          <div className="text-sm font-medium truncate" title={slot.color || 'Empty'}>{getShortName(slot.color)}</div>
        </div>
        <div className="text-xs text-farm-500">{slot.filament_type || 'PLA'}</div>
      </div>
      
      {/* Modal overlay for editing */}
      {isEditing && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          {/* Backdrop */}
          <div 
            className="absolute inset-0 bg-black/50" 
            onClick={() => { setIsEditing(false); setSearch('') }}
          />
          {/* Modal */}
          <div className="relative bg-farm-800 rounded-xl p-4 w-80 shadow-xl border border-farm-600">
            <div className="text-sm font-medium text-farm-300 mb-3">Slot {slot.slot_number} - Select Filament</div>
            <div className="relative mb-3">
              <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-farm-500" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search filaments..."
                className="w-full bg-farm-900 border border-farm-600 rounded-lg pl-8 pr-3 py-2 text-sm"
                autoFocus
              />
            </div>
            <div className="max-h-96 overflow-y-auto space-y-1">
              {filteredSpools.length > 0 && (
                <>
                  <div className="text-xs text-green-400 font-medium px-1 py-1">Tracked Spools</div>
                  {filteredSpools.map(s => (
                    <button
                      key={s.id}
                      onClick={() => handleSelectSpool(s)}
                      className="w-full flex items-center gap-2 px-2 py-2 hover:bg-farm-700 rounded-lg text-left text-sm"
                    >
                      <div 
                        className="w-5 h-5 rounded border border-farm-500 flex-shrink-0" 
                        style={{ backgroundColor: s.filament_color_hex ? `#${s.filament_color_hex}` : "#666" }}
                      />
                      <span className="truncate flex-1">{s.filament_brand} {s.filament_name}</span>
                      <span className="text-xs text-farm-400">{Math.round(s.remaining_weight_g)}g</span>
                    </button>
                  ))}
                </>
              )}
              {spoolmanFilaments.length > 0 && (
                <>
                  <div className="text-xs text-print-400 font-medium px-1 py-1">From Spoolman</div>
                  {spoolmanFilaments.slice(0, 15).map(f => (
                    <button
                      key={f.id}
                      onClick={() => handleSelect(f)}
                      className="w-full flex items-center gap-2 px-2 py-2 hover:bg-farm-700 rounded-lg text-left text-sm"
                    >
                      <div 
                        className="w-5 h-5 rounded border border-farm-500 flex-shrink-0" 
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
                  <div className="text-xs text-farm-400 font-medium px-1 py-1 mt-2">From Library</div>
                  {libraryFilaments.slice(0, 20).map(f => (
                    <button
                      key={f.id}
                      onClick={() => handleSelect(f)}
                      className="w-full flex items-center gap-2 px-2 py-2 hover:bg-farm-700 rounded-lg text-left text-sm"
                    >
                      <div 
                        className="w-5 h-5 rounded border border-farm-500 flex-shrink-0" 
                        style={{ backgroundColor: f.color_hex ? `#${f.color_hex}` : '#666' }}
                      />
                      <span className="truncate">{f.display_name}</span>
                    </button>
                  ))}
                </>
              )}
              {filteredFilaments.length === 0 && (
                <div className="text-sm text-farm-500 px-1 py-4 text-center">No filaments found</div>
              )}
            </div>
            <div className="flex gap-2 mt-4">
              <button onClick={handleClear} className="flex-1 text-sm bg-farm-700 hover:bg-farm-600 rounded-lg py-2">Clear</button>
              <button onClick={() => { setIsEditing(false); setSearch('') }} className="flex-1 text-sm bg-farm-700 hover:bg-farm-600 rounded-lg py-2">Cancel</button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

function PrinterCard({ printer, allFilaments, spools, onDelete, onToggleActive, onUpdateSlot, onEdit, onSyncAms, isDragging, onDragStart, onDragOver, onDragEnd }) {
  const [syncing, setSyncing] = useState(false)
  
  const handleSyncAms = async () => {
    setSyncing(true)
    try {
      await onSyncAms(printer.id)
    } finally {
      setSyncing(false)
    }
  }
  
  const hasBambuConnection = printer.api_type === 'bambu' && printer.api_host && printer.api_key
  
  // Count slots needing attention (assigned but not confirmed, or no spool assigned)
  const slotsNeedingAttention = printer.filament_slots?.filter(s => 
    (s.assigned_spool_id && !s.spool_confirmed) || (!s.assigned_spool_id && s.color_hex)
  ).length || 0
  
  return (
    <div 
      className={clsx(
        "bg-farm-900 rounded-xl border overflow-hidden h-fit transition-all",
        isDragging ? "border-print-500 opacity-50 scale-95" : "border-farm-800"
      )}
      draggable
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDragEnd={onDragEnd}
    >
      <div className="p-4 border-b border-farm-800 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="cursor-grab active:cursor-grabbing text-farm-600 hover:text-farm-400">
            <GripVertical size={18} />
          </div>
          <div>
            <h3 className="font-display font-semibold text-lg">{printer.name}</h3>
            <p className="text-sm text-farm-500">{printer.model || 'Unknown model'}</p>
          </div>
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
          {slotsNeedingAttention > 0 && (
            <span className="flex items-center gap-1 text-xs text-yellow-400" title="Slots need spool assignment">
              <AlertTriangle size={14} />
              {slotsNeedingAttention}
            </span>
          )}
          {hasBambuConnection ? (
            <button 
              onClick={handleSyncAms}
              disabled={syncing}
              className="ml-auto text-xs px-2 py-1 bg-farm-700 hover:bg-farm-600 rounded transition-colors disabled:opacity-50"
              title="Sync filament state from printer"
            >
              {syncing ? '⟳ Syncing...' : '↻ Sync AMS'}
            </button>
          ) : (
            <span className="text-xs text-farm-600 ml-auto">(click to edit)</span>
          )}
        </div>
        <div className={clsx(
          "grid gap-2",
          printer.filament_slots?.length <= 4 ? "grid-cols-4" : "grid-cols-4"
        )}>
          {printer.filament_slots?.map((slot) => (
            <FilamentSlotEditor 
              printerId={printer.id}
              spools={spools}
              
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
          <div className="flex items-center gap-2">
            {hasBambuConnection && (
              <span className="text-xs text-print-400">● Connected</span>
            )}
            <span className={printer.is_active ? 'text-print-400' : 'text-farm-500'}>{printer.is_active ? 'Active' : 'Inactive'}</span>
          </div>
        </div>
      </div>
    </div>
  )
}

function PrinterModal({ isOpen, onClose, onSubmit, printer, onSyncAms }) {
  const [formData, setFormData] = useState({ 
    name: '', 
    model: '', 
    slot_count: 4,
    api_type: '',
    api_host: '',
    serial: '',
    access_code: ''
  })
  const [testStatus, setTestStatus] = useState(null) // null, 'testing', 'success', 'error'
  const [testMessage, setTestMessage] = useState('')

  useEffect(() => {
    if (printer) {
      // Parse existing api_key if it's Bambu format (serial|access_code)
      let serial = ''
      let access_code = ''
      if (printer.api_key && printer.api_key.includes('|')) {
        const parts = printer.api_key.split('|')
        serial = parts[0] || ''
        access_code = parts[1] || ''
      }
      setFormData({ 
        name: printer.name || '', 
        model: printer.model || '', 
        slot_count: printer.slot_count || 4,
        api_type: printer.api_type || '',
        api_host: printer.api_host || '',
        serial: serial,
        access_code: access_code
      })
    } else {
      setFormData({ name: '', model: '', slot_count: 4, api_type: '', api_host: '', serial: '', access_code: '' })
    }
    setTestStatus(null)
    setTestMessage('')
  }, [printer, isOpen])

  const handleTestConnection = async () => {
    if (!formData.api_host || !formData.serial || !formData.access_code) {
      setTestStatus('error')
      setTestMessage('Please fill in IP, Serial, and Access Code')
      return
    }
    
    setTestStatus('testing')
    setTestMessage('Connecting to printer...')
    
    try {
      const response = await fetch('/api/printers/test-connection', {
        method: 'POST',
        headers: apiHeaders,
        body: JSON.stringify({
          api_type: formData.api_type || 'bambu',
          api_host: formData.api_host,
          serial: formData.serial,
          access_code: formData.access_code
        })
      })
      const result = await response.json()
      
      if (result.success) {
        setTestStatus('success')
        setTestMessage(`Connected! State: ${result.state}, Bed: ${result.bed_temp}°C, ${result.ams_slots || 0} AMS slots`)
      } else {
        setTestStatus('error')
        setTestMessage(result.error || 'Connection failed')
      }
    } catch (err) {
      setTestStatus('error')
      setTestMessage('Failed to test connection')
    }
  }

  const handleSubmit = (e) => {
    e.preventDefault()
    
    // Build the submit data
    const submitData = {
      name: formData.name,
      model: formData.model,
      slot_count: formData.slot_count
    }
    
    // Add connection info if provided
    if (formData.api_type) {
      submitData.api_type = formData.api_type
    }
    if (formData.api_host) {
      submitData.api_host = formData.api_host
    }
    if (formData.serial && formData.access_code) {
      submitData.api_key = `${formData.serial}|${formData.access_code}`
    }
    
    onSubmit(submitData, printer?.id)
  }

  if (!isOpen) return null

  const isEditing = !!printer
  const showBambuFields = formData.api_type === 'bambu'

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-farm-900 rounded-xl w-full max-w-md p-6 border border-farm-700 max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-display font-semibold">{isEditing ? 'Edit Printer' : 'Add New Printer'}</h2>
          <button onClick={onClose} className="text-farm-500 hover:text-farm-300"><X size={20} /></button>
        </div>
        <form onSubmit={handleSubmit} className="space-y-4">
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
          
          {/* Printer Connection Section */}
          <div className="border-t border-farm-700 pt-4 mt-4">
            <label className="block text-sm text-farm-400 mb-2">Printer Connection (Optional)</label>
            <select 
              value={formData.api_type} 
              onChange={(e) => setFormData(prev => ({ ...prev, api_type: e.target.value }))} 
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2"
            >
              <option value="">Manual (no connection)</option>
              <option value="bambu">Bambu Lab (X1C, P1S, A1, etc.)</option>
              <option value="octoprint" disabled>OctoPrint (coming soon)</option>
              <option value="moonraker" disabled>Moonraker/Klipper (coming soon)</option>
            </select>
          </div>
          
          {showBambuFields && (
            <>
              <div>
                <label className="block text-sm text-farm-400 mb-1">Printer IP Address</label>
                <input 
                  type="text" 
                  value={formData.api_host} 
                  onChange={(e) => setFormData(prev => ({ ...prev, api_host: e.target.value }))} 
                  className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2" 
                  placeholder="e.g., 192.168.1.100" 
                />
                <p className="text-xs text-farm-500 mt-1">Find this on the printer: Network settings</p>
              </div>
              <div>
                <label className="block text-sm text-farm-400 mb-1">Serial Number</label>
                <input 
                  type="text" 
                  value={formData.serial} 
                  onChange={(e) => setFormData(prev => ({ ...prev, serial: e.target.value }))} 
                  className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 font-mono" 
                  placeholder="e.g., 00M09A380700000" 
                />
                <p className="text-xs text-farm-500 mt-1">Find this on the printer: Settings → Device Info</p>
              </div>
              <div>
                <label className="block text-sm text-farm-400 mb-1">Access Code</label>
                <input 
                  type="text" 
                  value={formData.access_code} 
                  onChange={(e) => setFormData(prev => ({ ...prev, access_code: e.target.value }))} 
                  className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 font-mono" 
                  placeholder="e.g., 12345678" 
                />
                <p className="text-xs text-farm-500 mt-1">Find this on the printer: Settings → Network → Access Code</p>
              </div>
              
              {/* Test Connection Button */}
              <div>
                <button 
                  type="button" 
                  onClick={handleTestConnection}
                  disabled={testStatus === 'testing'}
                  className={clsx(
                    "w-full px-4 py-2 rounded-lg transition-colors flex items-center justify-center gap-2",
                    testStatus === 'testing' && "bg-farm-700 text-farm-400 cursor-wait",
                    testStatus === 'success' && "bg-green-900/50 text-green-400 border border-green-700",
                    testStatus === 'error' && "bg-red-900/50 text-red-400 border border-red-700",
                    !testStatus && "bg-farm-700 hover:bg-farm-600 text-farm-200"
                  )}
                >
                  {testStatus === 'testing' ? (
                    <>
                      <span className="animate-spin">⟳</span> Testing...
                    </>
                  ) : testStatus === 'success' ? (
                    '✓ Connected!'
                  ) : testStatus === 'error' ? (
                    '✗ Failed'
                  ) : (
                    'Test Connection'
                  )}
                </button>
                {testMessage && (
                  <p className={clsx(
                    "text-xs mt-1",
                    testStatus === 'success' ? "text-green-400" : testStatus === 'error' ? "text-red-400" : "text-farm-400"
                  )}>
                    {testMessage}
                  </p>
                )}
              </div>
            </>
          )}
          
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
  const [orderedPrinters, setOrderedPrinters] = useState([])
  const [draggedId, setDraggedId] = useState(null)

  const { data: printersData, isLoading } = useQuery({ queryKey: ['printers'], queryFn: () => printers.list() })
  const { data: filamentsData } = useQuery({ queryKey: ['filaments-combined'], queryFn: () => filaments.combined() })
  const { data: spoolsData } = useQuery({ queryKey: ['spools'], queryFn: async () => {
    const res = await fetch('/api/spools?status=active', { headers: { 'X-API-Key': '5464389e808f206efd9f9febef7743ff7a16911797cb0f058e805c82b33396ce' }})
    return res.json()
  }})
  
  const createPrinter = useMutation({ mutationFn: printers.create, onSuccess: () => { queryClient.invalidateQueries(['printers']); setShowModal(false) } })
  const updatePrinter = useMutation({ mutationFn: ({ id, data }) => printers.update(id, data), onSuccess: () => { queryClient.invalidateQueries(['printers']); setShowModal(false); setEditingPrinter(null) } })
  const deletePrinter = useMutation({ mutationFn: printers.delete, onSuccess: () => queryClient.invalidateQueries(['printers']) })
  const updateSlot = useMutation({ mutationFn: ({ printerId, slotNumber, data }) => printers.updateSlot(printerId, slotNumber, data), onSuccess: () => queryClient.invalidateQueries(['printers']) })
  const reorderPrinters = useMutation({ mutationFn: printers.reorder, onSuccess: () => queryClient.invalidateQueries(['printers']) })

  // Sync ordered printers with data
  useEffect(() => {
    if (printersData) {
      setOrderedPrinters(printersData)
    }
  }, [printersData])

  const handleDragStart = (e, printerId) => {
    setDraggedId(printerId)
    e.dataTransfer.effectAllowed = 'move'
  }

  const handleDragOver = (e, targetId) => {
    e.preventDefault()
    if (draggedId === null || draggedId === targetId) return
    const draggedIndex = orderedPrinters.findIndex(p => p.id === draggedId)
    const targetIndex = orderedPrinters.findIndex(p => p.id === targetId)
    if (draggedIndex === targetIndex) return
    const newOrder = [...orderedPrinters]
    const [dragged] = newOrder.splice(draggedIndex, 1)
    newOrder.splice(targetIndex, 0, dragged)
    setOrderedPrinters(newOrder)
  }

  const handleDragEnd = () => {
    if (draggedId !== null) {
      reorderPrinters.mutate(orderedPrinters.map(p => p.id))
    }
    setDraggedId(null)
  }

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

  const handleSyncAms = async (printerId) => {
    try {
      const response = await fetch(`/api/printers/${printerId}/sync-ams`, {
        method: 'POST',
        headers: apiHeaders
      })
      const result = await response.json()
      if (response.ok) {
        queryClient.invalidateQueries(['printers'])
      } else {
        alert(result.detail || 'Sync failed')
      }
    } catch (err) {
      alert('Failed to sync AMS')
    }
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
          {orderedPrinters?.map((printer) => (
            <PrinterCard 
              key={printer.id} 
              printer={printer} 
              allFilaments={filamentsData}
              spools={spoolsData}
              onDelete={(id) => { if (confirm('Delete this printer?')) deletePrinter.mutate(id) }} 
              onToggleActive={(id, active) => updatePrinter.mutate({ id, data: { is_active: active } })} 
              onUpdateSlot={(pid, slot, data) => updateSlot.mutate({ printerId: pid, slotNumber: slot, data })} 
              onEdit={handleEdit}
              onSyncAms={handleSyncAms}
              isDragging={draggedId === printer.id}
              onDragStart={(e) => handleDragStart(e, printer.id)}
              onDragOver={(e) => handleDragOver(e, printer.id)}
              onDragEnd={handleDragEnd}
            />
          ))}
        </div>
      )}
      <PrinterModal isOpen={showModal} onClose={handleCloseModal} onSubmit={handleSubmit} printer={editingPrinter} />
    </div>
  )
}
