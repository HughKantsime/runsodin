import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Package, Printer, QrCode, Scale, Archive, AlertTriangle, X, Pencil, Trash2, Beaker, Palette, Droplets, CheckSquare, Search } from 'lucide-react'
import clsx from 'clsx'
import toast from 'react-hot-toast'
import { canDo } from '../permissions'
import { useOrg } from '../contexts/OrgContext'
import { bulkOps } from '../api'
import ConfirmModal from '../components/ConfirmModal'

const API_KEY = import.meta.env.VITE_API_KEY
const API_BASE = '/api'

const apiHeaders = {
  'Content-Type': 'application/json',
  'X-API-Key': API_KEY
}

// API functions
async function checkedFetch(url, options) {
  const res = await fetch(url, options)
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || `Request failed (${res.status})`)
  }
  return res.json()
}

const spoolsApi = {
  list: async (filters = {}) => {
    const params = new URLSearchParams()
    if (filters.status) params.append('status', filters.status)
    if (filters.printer_id) params.append('printer_id', filters.printer_id)
    if (filters.org_id != null) params.append('org_id', filters.org_id)
    return checkedFetch(`${API_BASE}/spools?${params}`, { headers: apiHeaders })
  },
  get: async (id) => {
    return checkedFetch(`${API_BASE}/spools/${id}`, { headers: apiHeaders })
  },
  create: async (data) => {
    return checkedFetch(`${API_BASE}/spools`, {
      method: 'POST',
      headers: apiHeaders,
      body: JSON.stringify(data)
    })
  },
  update: async ({ id, ...data }) => {
    return checkedFetch(`${API_BASE}/spools/${id}`, {
      method: 'PATCH',
      headers: apiHeaders,
      body: JSON.stringify(data)
    })
  },
  load: async ({ id, printer_id, slot_number }) => {
    return checkedFetch(`${API_BASE}/spools/${id}/load`, {
      method: 'POST',
      headers: apiHeaders,
      body: JSON.stringify({ printer_id, slot_number })
    })
  },
  unload: async ({ id, storage_location }) => {
    return checkedFetch(`${API_BASE}/spools/${id}/unload?storage_location=${storage_location || ''}`, {
      method: 'POST',
      headers: apiHeaders
    })
  },
  use: async ({ id, weight_used_g, notes }) => {
    return checkedFetch(`${API_BASE}/spools/${id}/use`, {
      method: 'POST',
      headers: apiHeaders,
      body: JSON.stringify({ weight_used_g, notes })
    })
  },
  archive: async (id) => {
    return checkedFetch(`${API_BASE}/spools/${id}`, {
      method: 'DELETE',
      headers: apiHeaders
    })
  }
}

const filamentApi = {
  list: async () => {
    const res = await fetch(`${API_BASE}/filaments`, { headers: apiHeaders })
    return res.json()
  },
  create: async (data) => {
    const res = await fetch(`${API_BASE}/filaments`, {
      method: 'POST',
      headers: apiHeaders,
      body: JSON.stringify(data)
    })
    return res.json()
  },
  update: async ({ id, ...data }) => {
    const res = await fetch(`${API_BASE}/filaments/${id}`, {
      method: 'PATCH',
      headers: apiHeaders,
      body: JSON.stringify(data)
    })
    return res.json()
  },
  remove: async (id) => {
    const res = await fetch(`${API_BASE}/filaments/${id}`, {
      method: 'DELETE',
      headers: apiHeaders
    })
    if (!res.ok) {
      const err = await res.json()
      throw new Error(err.detail || 'Delete failed')
    }
    return true
  }
}

const printersApi = {
  list: async () => {
    const res = await fetch(`${API_BASE}/printers`, { headers: apiHeaders })
    return res.json()
  }
}

// ==================== Spool Components ====================

const HYGROSCOPIC_TYPES = new Set([
  'PA', 'NYLON_CF', 'NYLON_GF', 'PPS', 'PPS_CF',
  'PETG', 'PETG_CF', 'PC', 'PC_ABS', 'PC_CF', 'TPU', 'PVA',
])

function SpoolCard({ spool, onLoad, onUnload, onUse, onArchive, onEdit, onDry, printers }) {
  const percentRemaining = spool.percent_remaining || 0
  const isLow = percentRemaining < 20
  const isEmpty = spool.status === 'empty'
  const isArchived = spool.status === 'archived'
  
  const statusColor = isEmpty ? 'bg-red-500' : isLow ? 'bg-yellow-500' : 'bg-green-500'
  
  return (
    <div className={clsx(
      "bg-farm-900 rounded-lg p-3 md:p-4 border border-farm-800 hover:border-farm-700 transition-colors",
      isArchived && "opacity-50"
    )}>
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-center gap-2 md:gap-3 min-w-0">
          {spool.filament_color_hex && (
            <div 
              className="w-7 h-7 md:w-8 md:h-8 rounded-full border-2 border-farm-700 flex-shrink-0"
              style={{ backgroundColor: `#${spool.filament_color_hex}` }}
            />
          )}
          <div className="min-w-0">
            <h3 className="font-medium text-farm-100 text-sm md:text-base truncate">
              {spool.filament_brand} {spool.filament_name}
            </h3>
            <p className="text-xs md:text-sm text-farm-400">{spool.filament_material}</p>
          </div>
        </div>
        <span className={clsx(
          "px-2 py-1 rounded-lg text-xs font-medium flex-shrink-0 ml-2",
          spool.status === 'active' ? "bg-green-500/20 text-green-400" :
          spool.status === 'empty' ? "bg-red-500/20 text-red-400" :
          "bg-farm-700/30 text-farm-400"
        )}>
          {spool.status}
        </span>
      </div>
      
      {/* Weight bar */}
      <div className="mb-3">
        <div className="flex justify-between text-xs md:text-sm mb-1">
          <span className="text-farm-400">Remaining</span>
          <span className="text-farm-200">{spool.remaining_weight_g?.toFixed(0)}g / {spool.initial_weight_g?.toFixed(0)}g</span>
        </div>
        <div className="h-2 bg-farm-800 rounded-full overflow-hidden">
          <div 
            className={clsx("h-full transition-all", statusColor)}
            style={{ width: `${percentRemaining}%` }}
          />
        </div>
      </div>
      
      {/* Location */}
      <div className="text-xs md:text-sm text-farm-400 mb-3">
        {spool.location_printer_id ? (
          <span className="flex items-center gap-1">
            <Printer size={14} />
            {printers?.find(p => p.id === spool.location_printer_id)?.nickname || printers?.find(p => p.id === spool.location_printer_id)?.name || `Printer ${spool.location_printer_id}`}, Slot {spool.location_slot}
          </span>
        ) : spool.storage_location ? (
          <span className="flex items-center gap-1">
            <Package size={14} />
            {spool.storage_location}
          </span>
        ) : (
          <span className="text-farm-500">No location set</span>
        )}
      </div>
      
      {/* QR Code */}
      <div className="text-xs text-farm-500 mb-3 font-mono truncate">
        {spool.qr_code}
      </div>

      {/* Hygroscopic indicator */}
      {HYGROSCOPIC_TYPES.has(spool.filament_material) && (
        <div className="flex items-center gap-1.5 mb-3 text-xs text-amber-400">
          <Droplets size={12} />
          <span>Hygroscopic — dry before use</span>
        </div>
      )}

      {/* Actions */}
      <div className="flex gap-1.5 md:gap-2 flex-wrap">
        {canDo('spools.edit') && spool.location_printer_id ? (
          <button
            onClick={() => onUnload(spool)}
            className="px-2 md:px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-xs md:text-sm text-farm-200 flex items-center justify-center gap-1"
            title="Unload from printer"
          >
            <Package size={14} />
            <span className="hidden lg:inline">Unload</span>
          </button>
        ) : canDo('spools.edit') ? (
          <button
            onClick={() => onLoad(spool)}
            className="px-2 md:px-3 py-1.5 bg-print-600 hover:bg-print-500 rounded-lg text-xs md:text-sm text-white flex items-center justify-center gap-1"
            title="Load into printer"
          >
            <Printer size={14} />
            <span className="hidden lg:inline">Load</span>
          </button>
        ) : null}
        <a
          href={`${API_BASE}/spools/${spool.id}/label`}
          target="_blank"
          rel="noopener noreferrer"
          className="px-2 md:px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-xs md:text-sm text-farm-200 flex items-center justify-center gap-1"
        >
          <QrCode size={14} />
          <span className="hidden lg:inline">Label</span>
        </a>
        {canDo('spools.edit') && <button
          onClick={() => onEdit(spool)}
          className="px-2 md:px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-xs md:text-sm text-farm-200 flex items-center justify-center gap-1"
          title="Edit spool"
        >
          <Pencil size={14} />
          <span className="hidden lg:inline">Edit</span>
        </button>}
        {canDo('spools.edit') && <button
          onClick={() => onUse(spool)}
          className="px-2 md:px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-xs md:text-sm text-farm-200 flex items-center justify-center gap-1"
          title="Record usage"
        >
          <Scale size={14} />
          <span className="hidden lg:inline">Use</span>
        </button>}
        {canDo('spools.edit') && <button
          onClick={() => onDry(spool)}
          className="px-2 md:px-3 py-1.5 bg-farm-800 hover:bg-amber-900 rounded-lg text-xs md:text-sm text-farm-200 hover:text-amber-400 flex items-center justify-center gap-1"
          title="Log drying session"
        >
          <Droplets size={14} />
          <span className="hidden lg:inline">Dry</span>
        </button>}
        {canDo('spools.delete') && spool.status !== 'archived' && (
          <button
            onClick={() => onArchive(spool)}
            className="px-2 md:px-3 py-1.5 bg-farm-800 hover:bg-red-900 rounded-lg text-xs md:text-sm text-farm-200 hover:text-red-400 flex items-center justify-center gap-1"
            title="Archive"
          >
            <Archive size={14} />
            <span className="hidden lg:inline">Archive</span>
          </button>
        )}
      </div>
    </div>
  )
}

function CreateSpoolModal({ filaments, onClose, onCreate }) {
  const [form, setForm] = useState({
    filament_id: '',
    initial_weight_g: 1000,
    spool_weight_g: 250,
    vendor: '',
    price: '',
    storage_location: ''
  })

  useEffect(() => {
    const handleKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [onClose])

  const handleSubmit = (e) => {
    e.preventDefault()
    onCreate({
      ...form,
      filament_id: parseInt(form.filament_id),
      price: form.price ? parseFloat(form.price) : null
    })
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4" role="dialog" aria-modal="true" aria-labelledby="add-spool-title" onClick={onClose}>
      <div className="bg-farm-900 rounded-t-xl sm:rounded-lg p-5 md:p-6 w-full sm:max-w-md border border-farm-700 max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 id="add-spool-title" className="text-lg md:text-xl font-semibold text-farm-100">Add New Spool</h2>
          <button onClick={onClose} className="text-farm-400 hover:text-farm-200" aria-label="Close">
            <X size={20} />
          </button>
        </div>
        
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Filament Type</label>
            <select
              value={form.filament_id}
              onChange={(e) => setForm({ ...form, filament_id: e.target.value })}
              required
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
            >
              <option value="">Select filament...</option>
              {filaments?.map(f => (
                <option key={f.id} value={f.id.replace('lib_', '')}>
                  {f.brand} - {f.name} ({f.material})
                </option>
              ))}
            </select>
          </div>
          
          <div className="grid grid-cols-2 gap-3 md:gap-4">
            <div>
              <label className="block text-sm text-farm-400 mb-1">Net Weight (g)</label>
              <input
                type="number"
                value={form.initial_weight_g}
                onChange={(e) => setForm({ ...form, initial_weight_g: parseInt(e.target.value) })}
                className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
              />
            </div>
            <div>
              <label className="block text-sm text-farm-400 mb-1">Spool Weight (g)</label>
              <input
                type="number"
                value={form.spool_weight_g}
                onChange={(e) => setForm({ ...form, spool_weight_g: parseInt(e.target.value) })}
                className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
              />
            </div>
          </div>
          
          <div className="grid grid-cols-2 gap-3 md:gap-4">
            <div>
              <label className="block text-sm text-farm-400 mb-1">Vendor</label>
              <input
                type="text"
                value={form.vendor}
                onChange={(e) => setForm({ ...form, vendor: e.target.value })}
                placeholder="Amazon, MatterHackers..."
                className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
              />
            </div>
            <div>
              <label className="block text-sm text-farm-400 mb-1">Price</label>
              <input
                type="number"
                step="0.01"
                value={form.price}
                onChange={(e) => setForm({ ...form, price: e.target.value })}
                placeholder="24.99"
                className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
              />
            </div>
          </div>
          
          <div>
            <label className="block text-sm text-farm-400 mb-1">Storage Location</label>
            <input
              type="text"
              value={form.storage_location}
              onChange={(e) => setForm({ ...form, storage_location: e.target.value })}
              placeholder="Shelf A1, Dry Box 2..."
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
            />
          </div>
          
          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg text-farm-200"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="flex-1 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-white"
            >
              Add Spool
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function LoadSpoolModal({ spool, printers, onClose, onLoad }) {
  const [form, setForm] = useState({
    printer_id: '',
    slot_number: ''
  })

  useEffect(() => {
    const handleKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [onClose])

  const selectedPrinter = printers?.find(p => p.id === parseInt(form.printer_id))

  const handleSubmit = (e) => {
    e.preventDefault()
    onLoad({
      id: spool.id,
      printer_id: parseInt(form.printer_id),
      slot_number: parseInt(form.slot_number)
    })
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4" role="dialog" aria-modal="true" aria-labelledby="load-spool-title" onClick={onClose}>
      <div className="bg-farm-900 rounded-t-xl sm:rounded-lg p-5 md:p-6 w-full sm:max-w-md border border-farm-700 max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 id="load-spool-title" className="text-lg md:text-xl font-semibold text-farm-100">Load Spool</h2>
          <button onClick={onClose} className="text-farm-400 hover:text-farm-200" aria-label="Close">
            <X size={20} />
          </button>
        </div>
        
        <div className="mb-4 p-3 bg-farm-800 rounded-lg flex items-center gap-3">
          {spool.filament_color_hex && (
            <div 
              className="w-6 h-6 rounded-full border border-farm-600 flex-shrink-0"
              style={{ backgroundColor: `#${spool.filament_color_hex}` }}
            />
          )}
          <span className="text-farm-200 truncate">
            {spool.filament_brand} {spool.filament_name}
          </span>
        </div>
        
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Printer</label>
            <select
              value={form.printer_id}
              onChange={(e) => setForm({ ...form, printer_id: e.target.value, slot_number: '' })}
              required
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
            >
              <option value="">Select printer...</option>
              {printers?.map(p => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </select>
          </div>
          
          {selectedPrinter && (
            <div>
              <label className="block text-sm text-farm-400 mb-1">Slot</label>
              <select
                value={form.slot_number}
                onChange={(e) => setForm({ ...form, slot_number: e.target.value })}
                required
                className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
              >
                <option value="">Select slot...</option>
                {Array.from({ length: selectedPrinter.slot_count }, (_, i) => i + 1).map(n => {
                  const slot = selectedPrinter.filament_slots?.find(s => s.slot_number === n)
                  return (
                    <option key={n} value={n}>
                      Slot {n} {slot?.color ? `(${slot.color})` : '(empty)'}
                    </option>
                  )
                })}
              </select>
            </div>
          )}
          
          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg text-farm-200"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="flex-1 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-white"
            >
              Load
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function UseSpoolModal({ spool, onClose, onUse }) {
  const [form, setForm] = useState({
    weight_used_g: '',
    notes: ''
  })

  useEffect(() => {
    const handleKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [onClose])

  const handleSubmit = (e) => {
    e.preventDefault()
    onUse({
      id: spool.id,
      weight_used_g: parseFloat(form.weight_used_g),
      notes: form.notes
    })
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4" role="dialog" aria-modal="true" aria-labelledby="record-usage-title" onClick={onClose}>
      <div className="bg-farm-900 rounded-t-xl sm:rounded-lg p-5 md:p-6 w-full sm:max-w-md border border-farm-700 max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 id="record-usage-title" className="text-lg md:text-xl font-semibold text-farm-100">Record Usage</h2>
          <button onClick={onClose} className="text-farm-400 hover:text-farm-200" aria-label="Close">
            <X size={20} />
          </button>
        </div>
        
        <div className="mb-4 p-3 bg-farm-800 rounded-lg">
          <div className="flex items-center gap-3 mb-2">
            {spool.filament_color_hex && (
              <div 
                className="w-6 h-6 rounded-full border border-farm-600 flex-shrink-0"
                style={{ backgroundColor: `#${spool.filament_color_hex}` }}
              />
            )}
            <span className="text-farm-200 truncate">
              {spool.filament_brand} {spool.filament_name}
            </span>
          </div>
          <div className="text-sm text-farm-400">
            Current: {spool.remaining_weight_g?.toFixed(0)}g remaining
          </div>
        </div>
        
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Weight Used (g)</label>
            <input
              type="number"
              step="0.1"
              value={form.weight_used_g}
              onChange={(e) => setForm({ ...form, weight_used_g: e.target.value })}
              required
              placeholder="50"
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
            />
          </div>
          
          <div>
            <label className="block text-sm text-farm-400 mb-1">Notes (optional)</label>
            <input
              type="text"
              value={form.notes}
              onChange={(e) => setForm({ ...form, notes: e.target.value })}
              placeholder="Print job, waste..."
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
            />
          </div>
          
          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg text-farm-200"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="flex-1 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-white"
            >
              Record Usage
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function DryingModal({ spool, onClose, onSubmit }) {
  const [form, setForm] = useState({ duration_hours: '', temp_c: '', method: 'dryer', notes: '' })
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (spool) {
      const token = localStorage.getItem('token')
      const headers = { 'Authorization': `Bearer ${token}` }
      if (API_KEY) headers['X-API-Key'] = API_KEY
      fetch(`${API_BASE}/spools/${spool.id}/drying-history`, { headers })
        .then(r => r.json())
        .then(setHistory)
        .catch(() => {})
        .finally(() => setLoading(false))
    }
  }, [spool])

  useEffect(() => {
    const handleKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [onClose])

  const handleSubmit = (e) => {
    e.preventDefault()
    onSubmit({ id: spool.id, ...form, duration_hours: parseFloat(form.duration_hours), temp_c: form.temp_c ? parseFloat(form.temp_c) : null })
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4" role="dialog" aria-modal="true" aria-labelledby="drying-session-title" onClick={onClose}>
      <div className="bg-farm-900 rounded-t-xl sm:rounded-lg p-5 md:p-6 w-full sm:max-w-md border border-farm-700 max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 id="drying-session-title" className="text-lg md:text-xl font-semibold text-farm-100">Log Drying Session</h2>
          <button onClick={onClose} className="text-farm-400 hover:text-farm-200" aria-label="Close"><X size={20} /></button>
        </div>

        <div className="mb-4 p-3 bg-farm-800 rounded-lg">
          <div className="flex items-center gap-3">
            {spool.filament_color_hex && (
              <div className="w-6 h-6 rounded-full border border-farm-600 flex-shrink-0" style={{ backgroundColor: `#${spool.filament_color_hex}` }} />
            )}
            <span className="text-farm-200 truncate">{spool.filament_brand} {spool.filament_name}</span>
            <span className="text-xs text-farm-500 ml-auto">{spool.filament_material}</span>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-farm-400 mb-1">Duration (hours) *</label>
              <input type="number" step="0.5" required value={form.duration_hours} onChange={(e) => setForm({ ...form, duration_hours: e.target.value })} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100" placeholder="4" />
            </div>
            <div>
              <label className="block text-sm text-farm-400 mb-1">Temperature (°C)</label>
              <input type="number" value={form.temp_c} onChange={(e) => setForm({ ...form, temp_c: e.target.value })} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100" placeholder="55" />
            </div>
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Method</label>
            <select value={form.method} onChange={(e) => setForm({ ...form, method: e.target.value })} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100">
              <option value="dryer">Filament Dryer</option>
              <option value="oven">Oven</option>
              <option value="desiccant">Desiccant Box</option>
            </select>
          </div>
          <div>
            <label className="block text-sm text-farm-400 mb-1">Notes</label>
            <input type="text" value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100" placeholder="Before printing nylon parts" />
          </div>
          <div className="flex gap-3 pt-2">
            <button type="button" onClick={onClose} className="flex-1 px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg text-farm-200">Cancel</button>
            <button type="submit" className="flex-1 px-4 py-2 bg-amber-600 hover:bg-amber-500 rounded-lg text-white">Log Drying</button>
          </div>
        </form>

        {/* History */}
        {history.length > 0 && (
          <div className="mt-4 border-t border-farm-700 pt-4">
            <h3 className="text-sm font-medium text-farm-300 mb-2">Drying History</h3>
            <div className="space-y-1.5 max-h-40 overflow-y-auto">
              {history.map(h => (
                <div key={h.id} className="flex items-center justify-between text-xs text-farm-400">
                  <span>{h.dried_at ? new Date(h.dried_at).toLocaleDateString() : '—'}</span>
                  <span>{h.duration_hours}h @ {h.temp_c ? h.temp_c + '°C' : '—'}</span>
                  <span className="capitalize">{h.method}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function EditSpoolModal({ spool, onClose, onSave }) {
  const [form, setForm] = useState({
    remaining_weight_g: spool?.remaining_weight_g || 0,
    notes: spool?.notes || '',
    storage_location: spool?.storage_location || '',
  });

  useEffect(() => {
    const handleKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [onClose])

  const handleSubmit = (e) => {
    e.preventDefault();
    onSave({
      id: spool.id,
      remaining_weight_g: parseFloat(form.remaining_weight_g),
      notes: form.notes || null,
      storage_location: form.storage_location || null,
    });
  };

  if (!spool) return null;

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4" role="dialog" aria-modal="true" aria-labelledby="edit-spool-title" onClick={onClose}>
      <div className="bg-farm-900 rounded-t-xl sm:rounded-lg p-5 md:p-6 w-full sm:max-w-md border border-farm-700 max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 id="edit-spool-title" className="text-lg md:text-xl font-semibold text-farm-100">Edit Spool</h2>
          <button onClick={onClose} className="text-farm-400 hover:text-farm-200" aria-label="Close">
            <X size={20} />
          </button>
        </div>
        
        <div className="mb-4 p-3 bg-farm-800 rounded-lg">
          <div className="flex items-center gap-3 mb-2">
            {spool.filament_color_hex && (
              <div 
                className="w-8 h-8 rounded-full border border-farm-600 flex-shrink-0"
                style={{ backgroundColor: '#' + spool.filament_color_hex }}
              />
            )}
            <div>
              <div className="text-farm-200 font-medium">
                {spool.filament_brand} {spool.filament_name}
              </div>
              <div className="text-xs text-farm-500 font-mono">{spool.qr_code}</div>
            </div>
          </div>
        </div>
        
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Remaining Weight (g)</label>
            <input
              type="number"
              step="1"
              min="0"
              value={form.remaining_weight_g}
              onChange={(e) => setForm({ ...form, remaining_weight_g: e.target.value })}
              required
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
            />
            <div className="text-xs text-farm-500 mt-1">
              Initial: {spool.initial_weight_g}g
            </div>
          </div>
          
          <div>
            <label className="block text-sm text-farm-400 mb-1">Storage Location</label>
            <input
              type="text"
              value={form.storage_location}
              onChange={(e) => setForm({ ...form, storage_location: e.target.value })}
              placeholder="Shelf A, Drawer 2..."
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
            />
          </div>
          
          <div>
            <label className="block text-sm text-farm-400 mb-1">Notes</label>
            <input
              type="text"
              value={form.notes}
              onChange={(e) => setForm({ ...form, notes: e.target.value })}
              placeholder="Any notes..."
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
            />
          </div>
          
          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg text-farm-200"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="flex-1 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-white"
            >
              Save
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ==================== Filament Library Components ====================

function EditFilamentModal({ filament, onClose, onSave }) {
  const [form, setForm] = useState({
    brand: filament?.brand || '',
    name: filament?.name || '',
    material: filament?.material || 'PLA',
    color_hex: filament?.color_hex || '',
    cost_per_gram: filament?.cost_per_gram || ''
  })

  useEffect(() => {
    const handleKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', handleKey)
    return () => document.removeEventListener('keydown', handleKey)
  }, [onClose])

  const handleSubmit = (e) => {
    e.preventDefault()
    onSave({ id: filament.id, ...form })
  }

  return (
    <div className="fixed inset-0 bg-black/50 flex items-end sm:items-center justify-center z-50 p-0 sm:p-4" role="dialog" aria-modal="true" aria-labelledby="filament-modal-title" onClick={onClose}>
      <div className="bg-farm-900 rounded-t-xl sm:rounded-lg p-5 md:p-6 w-full sm:max-w-md border border-farm-700 max-h-[90vh] overflow-y-auto" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 id="filament-modal-title" className="text-lg md:text-xl font-semibold text-farm-100">
            {filament ? 'Edit Filament' : 'Add Filament'}
          </h2>
          <button onClick={onClose} className="text-farm-400 hover:text-farm-200" aria-label="Close">
            <X size={20} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm text-farm-400 mb-1">Brand</label>
            <input
              type="text"
              value={form.brand}
              onChange={(e) => setForm({ ...form, brand: e.target.value })}
              required
              placeholder="Bambu Lab, Polymaker, eSUN..."
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
            />
          </div>

          <div>
            <label className="block text-sm text-farm-400 mb-1">Name</label>
            <input
              type="text"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              required
              placeholder="Matte Black, Silk Gold..."
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
            />
          </div>

          <div className="grid grid-cols-2 gap-3 md:gap-4">
            <div>
              <label className="block text-sm text-farm-400 mb-1">Material</label>
              <select
                value={form.material}
                onChange={(e) => setForm({ ...form, material: e.target.value })}
                className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
              >
                <option value="PLA">PLA</option>
                <option value="PLA-S">PLA-S (Support)</option>
                <option value="PLA-CF">PLA-CF</option>
                <option value="PETG">PETG</option>
                <option value="PETG_CF">PETG-CF</option>
                <option value="ABS">ABS</option>
                <option value="ASA">ASA</option>
                <option value="TPU">TPU</option>
                <option value="PA">Nylon (PA)</option>
                <option value="NYLON_CF">Nylon-CF</option>
                <option value="NYLON_GF">Nylon-GF</option>
                <option value="PC">Polycarbonate</option>
                <option value="PC_ABS">PC-ABS</option>
                <option value="PC_CF">PC-CF</option>
                <option value="PPS">PPS</option>
                <option value="PPS_CF">PPS-CF</option>
                <option value="PVA">PVA (Support)</option>
                <option value="HIPS">HIPS</option>
              </select>
            </div>
            <div>
              <label className="block text-sm text-farm-400 mb-1">Color Hex</label>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={form.color_hex}
                  onChange={(e) => setForm({ ...form, color_hex: e.target.value.replace('#', '') })}
                  placeholder="FF5733"
                  maxLength={6}
                  className="flex-1 bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100 font-mono"
                />
                {form.color_hex && form.color_hex.length >= 3 && (
                  <div
                    className="w-10 h-10 rounded-lg border border-farm-600 flex-shrink-0"
                    style={{ backgroundColor: `#${form.color_hex}` }}
                  />
                )}
              </div>
            </div>
          </div>

          <div>
            <label className="block text-sm text-farm-400 mb-1">Cost per Gram ($/g)</label>
            <input
              type="number"
              step="0.001"
              min="0"
              value={form.cost_per_gram}
              onChange={(e) => setForm({ ...form, cost_per_gram: e.target.value })}
              placeholder="0.025 (leave blank for global default)"
              className="w-full bg-farm-800 border border-farm-700 rounded-lg px-3 py-2 text-farm-100"
            />
            <p className="text-xs text-farm-500 mt-1">Used for cost calculation. Leave blank to use system default.</p>
          </div>

          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded-lg text-farm-200"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="flex-1 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-white"
            >
              {filament ? 'Save Changes' : 'Add Filament'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function FilamentLibraryView() {
  const queryClient = useQueryClient()
  const [editingFilament, setEditingFilament] = useState(null)
  const [showAddModal, setShowAddModal] = useState(false)
  const [deleteConfirm, setDeleteConfirm] = useState(null)
  const [materialFilter, setMaterialFilter] = useState('all')

  const { data: filaments, isLoading } = useQuery({
    queryKey: ['filaments'],
    queryFn: filamentApi.list
  })

  const createMutation = useMutation({
    mutationFn: filamentApi.create,
    onSuccess: () => {
      queryClient.invalidateQueries(['filaments'])
      setShowAddModal(false)
    }
  })

  const updateMutation = useMutation({
    mutationFn: filamentApi.update,
    onSuccess: () => {
      queryClient.invalidateQueries(['filaments'])
      setEditingFilament(null)
    }
  })

  const deleteMutation = useMutation({
    mutationFn: filamentApi.remove,
    onSuccess: () => {
      queryClient.invalidateQueries(['filaments'])
      setDeleteConfirm(null)
    }
  })

  // Get unique materials for filter
  const materials = [...new Set(filaments?.map(f => f.material) || [])]

  const filtered = filaments?.filter(f =>
    materialFilter === 'all' || f.material === materialFilter
  ) || []

  // Group by brand
  const grouped = {}
  filtered.forEach(f => {
    if (!grouped[f.brand]) grouped[f.brand] = []
    grouped[f.brand].push(f)
  })

  return (
    <>
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between mb-4 md:mb-6 gap-3">
        <div>
          <p className="text-farm-400 text-sm">{filaments?.length || 0} filament types in library</p>
        </div>
        <div className="flex items-center gap-3">
          <select
            value={materialFilter}
            onChange={(e) => setMaterialFilter(e.target.value)}
            className="bg-farm-800 border border-farm-700 rounded-lg px-3 py-1.5 text-sm text-farm-200"
          >
            <option value="all">All Materials</option>
            {materials.map(m => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
          {canDo('spools.edit') && (
            <button
              onClick={() => setShowAddModal(true)}
              className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-white text-sm"
            >
              <Plus size={16} />
              Add Filament
            </button>
          )}
        </div>
      </div>

      {isLoading && <div className="text-center text-farm-400 py-12">Loading filaments...</div>}

      {!isLoading && filtered.length === 0 && (
        <div className="text-center text-farm-500 py-12 text-sm">
          No filaments found. Add filament types to build your library.
        </div>
      )}

      {/* Filament table grouped by brand */}
      {Object.entries(grouped).sort(([a], [b]) => a.localeCompare(b)).map(([brand, brandFilaments]) => (
        <div key={brand} className="mb-6">
          <h3 className="text-sm font-semibold text-farm-300 mb-2 uppercase tracking-wide">{brand}</h3>
          <div className="bg-farm-900 rounded-lg border border-farm-800 overflow-hidden">
            {brandFilaments.map((f, idx) => (
              <div
                key={f.id}
                className={clsx(
                  "flex items-center justify-between px-3 md:px-4 py-2.5 md:py-3 gap-3",
                  idx > 0 && "border-t border-farm-800"
                )}
              >
                <div className="flex items-center gap-3 min-w-0 flex-1">
                  {f.color_hex ? (
                    <div
                      className="w-6 h-6 rounded-full border border-farm-600 flex-shrink-0"
                      style={{ backgroundColor: `#${f.color_hex}` }}
                    />
                  ) : (
                    <div className="w-6 h-6 rounded-full border border-farm-700 bg-farm-800 flex-shrink-0 flex items-center justify-center">
                      <Palette size={12} className="text-farm-500" />
                    </div>
                  )}
                  <div className="min-w-0">
                    <span className="text-sm text-farm-100 truncate block">{f.name}</span>
                  </div>
                  <span className="px-2 py-0.5 bg-farm-800 rounded-lg text-xs text-farm-400 flex-shrink-0">
                    {f.material}
                  </span>
                  {f.cost_per_gram && (
                    <span className="px-2 py-0.5 bg-green-900/50 rounded-lg text-xs text-green-400 flex-shrink-0">
                      ${f.cost_per_gram}/g
                    </span>
                  )}
                </div>

                {canDo('spools.edit') && (
                  <div className="flex items-center gap-1.5 flex-shrink-0">
                    <button
                      onClick={() => setEditingFilament(f)}
                      className="p-1.5 bg-farm-800 hover:bg-farm-700 rounded-lg text-farm-300 hover:text-farm-100 transition-colors"
                      title="Edit"
                    >
                      <Pencil size={14} />
                    </button>
                    {deleteConfirm === f.id ? (
                      <div className="flex items-center gap-1">
                        <button
                          onClick={() => deleteMutation.mutate(f.id)}
                          disabled={deleteMutation.isPending}
                          className="px-2 py-1 bg-red-600 hover:bg-red-500 rounded-lg text-white text-xs font-medium"
                        >
                          Delete
                        </button>
                        <button
                          onClick={() => setDeleteConfirm(null)}
                          className="px-2 py-1 bg-farm-700 hover:bg-farm-600 rounded-lg text-farm-200 text-xs"
                        >
                          No
                        </button>
                      </div>
                    ) : (
                      <button
                        onClick={() => setDeleteConfirm(f.id)}
                        className="p-1.5 bg-farm-800 hover:bg-red-900 rounded-lg text-farm-300 hover:text-red-400 transition-colors"
                        title="Delete"
                      >
                        <Trash2 size={14} />
                      </button>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      ))}

      {/* Modals */}
      {showAddModal && (
        <EditFilamentModal
          filament={null}
          onClose={() => setShowAddModal(false)}
          onSave={(data) => {
            const { id, ...rest } = data
            createMutation.mutate(rest)
          }}
        />
      )}

      {editingFilament && (
        <EditFilamentModal
          filament={editingFilament}
          onClose={() => setEditingFilament(null)}
          onSave={updateMutation.mutate}
        />
      )}
    </>
  )
}

// ==================== Main Page ====================

export default function Spools() {
  const org = useOrg()
  const queryClient = useQueryClient()
  const [view, setView] = useState('spools') // 'spools' | 'library'
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [loadingSpool, setLoadingSpool] = useState(null)
  const [usingSpool, setUsingSpool] = useState(null)
  const [editingSpool, setEditingSpool] = useState(null)
  const [dryingSpool, setDryingSpool] = useState(null)
  const [filter, setFilter] = useState('active')
  const [sortBy, setSortBy] = useState("printer")
  const [groupByPrinter, setGroupByPrinter] = useState(true)
  const [searchQuery, setSearchQuery] = useState('')
  
  const { data: spools, isLoading } = useQuery({
    queryKey: ['spools', filter, org.orgId],
    queryFn: () => spoolsApi.list({ status: filter === 'all' ? undefined : filter, org_id: org.orgId })
  })
  
  const { data: filaments } = useQuery({
    queryKey: ['filaments'],
    queryFn: filamentApi.list
  })
  
  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: printersApi.list
  })
  
  const createMutation = useMutation({
    mutationFn: spoolsApi.create,
    onSuccess: () => {
      queryClient.invalidateQueries(['spools'])
      setShowCreateModal(false)
      toast.success('Spool created')
    },
    onError: (err) => toast.error(err.message || 'Failed to create spool')
  })

  const loadMutation = useMutation({
    mutationFn: spoolsApi.load,
    onSuccess: () => {
      queryClient.invalidateQueries(['spools'])
      queryClient.invalidateQueries(['printers'])
      setLoadingSpool(null)
      toast.success('Spool loaded')
    },
    onError: (err) => toast.error(err.message || 'Failed to load spool')
  })

  const unloadMutation = useMutation({
    mutationFn: spoolsApi.unload,
    onSuccess: () => {
      queryClient.invalidateQueries(['spools'])
      queryClient.invalidateQueries(['printers'])
      toast.success('Spool unloaded')
    },
    onError: (err) => toast.error(err.message || 'Failed to unload spool')
  })

  const useMutation2 = useMutation({
    mutationFn: spoolsApi.use,
    onSuccess: () => {
      queryClient.invalidateQueries(['spools'])
      setUsingSpool(null)
      toast.success('Usage recorded')
    },
    onError: (err) => toast.error(err.message || 'Failed to record usage')
  })

  const archiveMutation = useMutation({
    mutationFn: spoolsApi.archive,
    onSuccess: () => {
      queryClient.invalidateQueries(['spools'])
      toast.success('Spool archived')
    },
    onError: (err) => toast.error(err.message || 'Failed to archive spool')
  })

  // Bulk selection
  const [selectedSpools, setSelectedSpools] = useState(new Set())
  const toggleSpoolSelect = (id) => setSelectedSpools(prev => {
    const next = new Set(prev)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })
  const toggleSelectAllSpools = (ids) => {
    setSelectedSpools(prev => prev.size === ids.length ? new Set() : new Set(ids))
  }
  const bulkSpoolAction = useMutation({
    mutationFn: ({ action }) => bulkOps.spools([...selectedSpools], action),
    onSuccess: () => { queryClient.invalidateQueries(['spools']); setSelectedSpools(new Set()) },
  })

  const [confirmAction, setConfirmAction] = useState(null)

  const handleUnload = (spool) => {
    setConfirmAction({
      title: 'Unload Spool',
      message: `Unload ${spool.filament_brand} ${spool.filament_name} from printer?`,
      onConfirm: () => { unloadMutation.mutate({ id: spool.id }); setConfirmAction(null) }
    })
  }

  const handleEditSpool = async (data) => {
    try {
      await spoolsApi.update(data);
      queryClient.invalidateQueries(["spools"]);
      setEditingSpool(null);
      toast.success('Spool updated')
    } catch (err) {
      toast.error(err.message || 'Failed to update spool')
    }
  };

  const handleArchive = (spool) => {
    setConfirmAction({
      title: 'Archive Spool',
      message: `Archive ${spool.filament_brand} ${spool.filament_name}? This will mark it as no longer in use.`,
      onConfirm: () => { archiveMutation.mutate(spool.id); setConfirmAction(null) }
    })
  }
  
  // Summary stats
  const activeSpools = spools?.filter(s => s.status === 'active') || []
  const lowSpools = activeSpools.filter(s => s.percent_remaining < 20)
  const loadedSpools = activeSpools.filter(s => s.location_printer_id)
  
  return (
    <div className="p-4 md:p-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between mb-4 md:mb-6 gap-3">
        <div>
          <h1 className="text-xl md:text-2xl font-display font-bold text-farm-100">Spools</h1>
          <p className="text-farm-400 mt-1 text-sm">Track your filament inventory</p>
        </div>
        <div className="flex items-center gap-2">
          {/* View toggle */}
          <div className="flex bg-farm-800 rounded-lg p-0.5">
            <button
              onClick={() => setView('spools')}
              className={clsx(
                "px-3 py-1.5 rounded-lg text-xs md:text-sm font-medium transition-colors",
                view === 'spools' ? "bg-print-600 text-white" : "text-farm-400 hover:text-farm-200"
              )}
            >
              Spools
            </button>
            <button
              onClick={() => setView('library')}
              className={clsx(
                "px-3 py-1.5 rounded-lg text-xs md:text-sm font-medium transition-colors flex items-center gap-1.5",
                view === 'library' ? "bg-print-600 text-white" : "text-farm-400 hover:text-farm-200"
              )}
            >
              <Beaker size={14} />
              Filament Library
            </button>
          </div>
          {view === 'spools' && canDo('spools.edit') && (
            <button
              onClick={() => setShowCreateModal(true)}
              className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-white text-sm"
            >
              <Plus size={18} />
              Add Spool
            </button>
          )}
        </div>
      </div>

      {/* Filament Library View */}
      {view === 'library' && <FilamentLibraryView />}

      {/* Spools View */}
      {view === 'spools' && (
        <>
          {/* Stats */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4 mb-4 md:mb-6">
            <div className="bg-farm-900 rounded-lg p-3 md:p-4 border border-farm-800">
              <div className="text-xl md:text-2xl font-bold text-farm-100">{activeSpools.length}</div>
              <div className="text-xs md:text-sm text-farm-400">Active Spools</div>
            </div>
            <div className="bg-farm-900 rounded-lg p-3 md:p-4 border border-farm-800">
              <div className="text-xl md:text-2xl font-bold text-print-400">{loadedSpools.length}</div>
              <div className="text-xs md:text-sm text-farm-400">Loaded</div>
            </div>
            <div className="bg-farm-900 rounded-lg p-3 md:p-4 border border-farm-800">
              <div className="text-xl md:text-2xl font-bold text-yellow-400">{lowSpools.length}</div>
              <div className="text-xs md:text-sm text-farm-400">Low (&lt;20%)</div>
            </div>
            <div className="bg-farm-900 rounded-lg p-3 md:p-4 border border-farm-800">
              <div className="text-xl md:text-2xl font-bold text-farm-100">
                {activeSpools.reduce((sum, s) => sum + (s.remaining_weight_g || 0), 0).toFixed(0)}g
              </div>
              <div className="text-xs md:text-sm text-farm-400">Total Filament</div>
            </div>
          </div>
          
          {/* Low warning */}
          {lowSpools.length > 0 && (
            <button
              onClick={() => { setFilter('active'); setSortBy('remaining') }}
              className="mb-4 md:mb-6 p-3 md:p-4 bg-yellow-500/10 border border-yellow-500/30 rounded-lg flex items-center gap-3 w-full text-left hover:bg-yellow-500/20 transition-colors"
            >
              <AlertTriangle className="text-yellow-500 flex-shrink-0" size={20} />
              <span className="text-yellow-200 text-sm md:text-base">
                {lowSpools.length} spool{lowSpools.length > 1 ? 's' : ''} running low on filament
              </span>
            </button>
          )}
          
          {/* Search */}
          <div className="mb-4 md:mb-6">
            <div className="relative">
              <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-farm-500" />
              <input
                type="text"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder="Search by brand, name, or material..."
                className="w-full bg-farm-800 border border-farm-700 rounded-lg pl-9 pr-3 py-2 text-sm text-farm-100 placeholder-farm-500"
              />
            </div>
          </div>

          {/* Filter tabs + Sort controls */}
          <div className="flex flex-col sm:flex-row sm:items-center gap-3 sm:gap-4 mb-4 md:mb-6">
            <div className="flex gap-1.5 md:gap-2 justify-evenly">
              {['active', 'empty', 'archived', 'all'].map(f => (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={clsx(
                    "px-3 md:px-4 py-1.5 md:py-2 rounded-lg text-xs md:text-sm font-medium transition-colors",
                    filter === f
                      ? "bg-print-600 text-white"
                      : "bg-farm-800 text-farm-400 hover:bg-farm-700"
                  )}
                >
                  {f.charAt(0).toUpperCase() + f.slice(1)}
                </button>
              ))}
            </div>
            
            <div className="flex gap-3 items-center sm:ml-auto">
              <span className="text-xs md:text-sm text-farm-400">Sort:</span>
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value)}
                className="bg-farm-800 border border-farm-700 rounded-lg px-2 md:px-3 py-1 md:py-1.5 text-xs md:text-sm text-farm-200"
              >
                <option value="printer">Printer/Slot</option>
                <option value="name">Name</option>
                <option value="remaining">Remaining %</option>
                <option value="material">Material</option>
              </select>
              <label className="flex items-center gap-1.5 md:gap-2 text-xs md:text-sm text-farm-400">
                <input
                  type="checkbox"
                  checked={groupByPrinter}
                  onChange={(e) => setGroupByPrinter(e.target.checked)}
                  className="rounded-lg bg-farm-800 border-farm-700"
                />
                <span className="hidden sm:inline">Group by printer</span>
                <span className="sm:hidden">Group</span>
              </label>
            </div>
          </div>
          
          {selectedSpools.size > 0 && canDo('spools.edit') && (
            <div className="flex items-center gap-3 mb-4 p-3 bg-print-900/50 border border-print-700 rounded-lg">
              <span className="text-sm text-farm-300">{selectedSpools.size} selected</span>
              <button onClick={() => bulkSpoolAction.mutate({ action: 'archive' })} className="px-3 py-1 bg-amber-600 hover:bg-amber-500 rounded text-xs">Archive</button>
              <button onClick={() => bulkSpoolAction.mutate({ action: 'activate' })} className="px-3 py-1 bg-green-600 hover:bg-green-500 rounded text-xs">Activate</button>
              <button onClick={() => setConfirmAction({ title: 'Delete Spools', message: `Delete ${selectedSpools.size} selected spool(s)? This cannot be undone.`, onConfirm: () => { bulkSpoolAction.mutate({ action: 'delete' }); setConfirmAction(null) } })} className="px-3 py-1 bg-red-600 hover:bg-red-500 rounded text-xs">Delete</button>
              <button onClick={() => setSelectedSpools(new Set())} className="px-3 py-1 bg-farm-700 hover:bg-farm-600 rounded text-xs">Clear</button>
            </div>
          )}
          {canDo('spools.edit') && spools?.length > 0 && (
            <div className="flex items-center gap-2 mb-3">
              <label className="flex items-center gap-1.5 text-xs text-farm-400 cursor-pointer">
                <input type="checkbox" checked={selectedSpools.size === spools.length && spools.length > 0} onChange={() => toggleSelectAllSpools(spools.map(s => s.id))} className="rounded border-farm-600" />
                Select all
              </label>
            </div>
          )}

          {isLoading && <div className="text-center text-farm-400 py-12">Loading spools...</div>}
          {!isLoading && spools?.length === 0 && <div className="text-center text-farm-400 py-12 text-sm md:text-base">No spools found. Add your first spool to get started!</div>}
          {!isLoading && spools?.length > 0 && (
          (() => {
            if (!spools) return null;
            let sorted = [...spools];

            // Apply text search
            if (searchQuery.trim()) {
              const q = searchQuery.toLowerCase()
              sorted = sorted.filter(s =>
                (s.filament_brand || '').toLowerCase().includes(q) ||
                (s.filament_name || '').toLowerCase().includes(q) ||
                (s.filament_material || '').toLowerCase().includes(q)
              )
            }
            
            if (sortBy === "printer") {
              sorted.sort((a, b) => {
                if (a.location_printer_id !== b.location_printer_id) return (a.location_printer_id || 999) - (b.location_printer_id || 999);
                return (a.location_slot || 999) - (b.location_slot || 999);
              });
            } else if (sortBy === "name") {
              sorted.sort((a, b) => `${a.filament_brand} ${a.filament_name}`.localeCompare(`${b.filament_brand} ${b.filament_name}`));
            } else if (sortBy === "remaining") {
              sorted.sort((a, b) => (a.percent_remaining || 0) - (b.percent_remaining || 0));
            } else if (sortBy === "material") {
              sorted.sort((a, b) => (a.filament_material || "").localeCompare(b.filament_material || ""));
            }
            
            if (groupByPrinter && sortBy === "printer") {
              const groups = {};
              sorted.forEach(s => {
                const key = s.location_printer_id ? (printers?.find(p => p.id === s.location_printer_id)?.nickname || printers?.find(p => p.id === s.location_printer_id)?.name || `Printer ${s.location_printer_id}`) : "Unassigned";
                if (!groups[key]) groups[key] = [];
                groups[key].push(s);
              });
              
              return Object.entries(groups).map(([group, groupSpools]) => (
                <div key={group} className="mb-4 md:mb-6">
                  <h3 className="text-base md:text-lg font-semibold text-farm-200 mb-3">{group}</h3>
                  <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3 md:gap-4">
                    {groupSpools.map(spool => (
                      <div key={spool.id} className="relative">
                        {canDo('spools.edit') && (
                          <input type="checkbox" checked={selectedSpools.has(spool.id)} onChange={() => toggleSpoolSelect(spool.id)} className="absolute top-3 left-3 z-10 rounded border-farm-600" />
                        )}
                        <div className={canDo('spools.edit') ? 'pl-7' : ''}>
                        <SpoolCard spool={spool} onLoad={setLoadingSpool} onUnload={handleUnload} onUse={setUsingSpool} onArchive={handleArchive} onEdit={setEditingSpool} onDry={setDryingSpool} printers={printers} />
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ));
            }
            
            return (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3 md:gap-4">
                {sorted.map(spool => (
                  <div key={spool.id} className="relative">
                    {canDo('spools.edit') && (
                      <input type="checkbox" checked={selectedSpools.has(spool.id)} onChange={() => toggleSpoolSelect(spool.id)} className="absolute top-3 left-3 z-10 rounded border-farm-600" />
                    )}
                    <SpoolCard spool={spool} onLoad={setLoadingSpool} onUnload={handleUnload} onUse={setUsingSpool} onArchive={handleArchive} onEdit={setEditingSpool} onDry={setDryingSpool} printers={printers} />
                  </div>
                ))}
              </div>
            );
          })())}
        </>
      )}
      
      {/* Spool Modals */}
      {showCreateModal && (
        <CreateSpoolModal
          filaments={filaments}
          onClose={() => setShowCreateModal(false)}
          onCreate={createMutation.mutate}
        />
      )}
      
      {loadingSpool && (
        <LoadSpoolModal
          spool={loadingSpool}
          printers={printers}
          onClose={() => setLoadingSpool(null)}
          onLoad={loadMutation.mutate}
        />
      )}
      
      {usingSpool && (
        <UseSpoolModal
          spool={usingSpool}
          onClose={() => setUsingSpool(null)}
          onUse={useMutation2.mutate}
        />
      )}
      {editingSpool && (
        <EditSpoolModal
          spool={editingSpool}
          onClose={() => setEditingSpool(null)}
          onSave={handleEditSpool}
        />
      )}
      {dryingSpool && (
        <DryingModal
          spool={dryingSpool}
          onClose={() => setDryingSpool(null)}
          onSubmit={async (data) => {
            try {
              const { id, ...rest } = data
              const token = localStorage.getItem('token')
              const headers = { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` }
              if (API_KEY) headers['X-API-Key'] = API_KEY
              const res = await fetch(`${API_BASE}/spools/${id}/dry`, {
                method: 'POST',
                headers,
                body: JSON.stringify(rest)
              })
              if (!res.ok) throw new Error('Failed to log drying session')
              toast.success('Drying session logged')
              setDryingSpool(null)
            } catch (err) {
              toast.error(err.message || 'Failed to log drying session')
            }
          }}
        />
      )}
      <ConfirmModal
        open={!!confirmAction}
        title={confirmAction?.title || ''}
        message={confirmAction?.message || ''}
        confirmText={confirmAction?.title?.includes('Delete') ? 'Delete' : 'Confirm'}
        onConfirm={() => confirmAction?.onConfirm()}
        onCancel={() => setConfirmAction(null)}
      />
    </div>
  )
}
