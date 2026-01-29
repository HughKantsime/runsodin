import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Package, Printer, QrCode, Scale, Archive, AlertTriangle, X } from 'lucide-react'
import clsx from 'clsx'

const API_KEY = '5464389e808f206efd9f9febef7743ff7a16911797cb0f058e805c82b33396ce'
const API_BASE = '/api'

const apiHeaders = {
  'Content-Type': 'application/json',
  'X-API-Key': API_KEY
}

// API functions
const spoolsApi = {
  list: async (filters = {}) => {
    const params = new URLSearchParams()
    if (filters.status) params.append('status', filters.status)
    if (filters.printer_id) params.append('printer_id', filters.printer_id)
    const res = await fetch(`${API_BASE}/spools?${params}`, { headers: apiHeaders })
    return res.json()
  },
  get: async (id) => {
    const res = await fetch(`${API_BASE}/spools/${id}`, { headers: apiHeaders })
    return res.json()
  },
  create: async (data) => {
    const res = await fetch(`${API_BASE}/spools`, {
      method: 'POST',
      headers: apiHeaders,
      body: JSON.stringify(data)
    })
    return res.json()
  },
  update: async ({ id, ...data }) => {
    const res = await fetch(`${API_BASE}/spools/${id}`, {
      method: 'PATCH',
      headers: apiHeaders,
      body: JSON.stringify(data)
    })
    return res.json()
  },
  load: async ({ id, printer_id, slot_number }) => {
    const res = await fetch(`${API_BASE}/spools/${id}/load`, {
      method: 'POST',
      headers: apiHeaders,
      body: JSON.stringify({ printer_id, slot_number })
    })
    return res.json()
  },
  unload: async ({ id, storage_location }) => {
    const res = await fetch(`${API_BASE}/spools/${id}/unload?storage_location=${storage_location || ''}`, {
      method: 'POST',
      headers: apiHeaders
    })
    return res.json()
  },
  use: async ({ id, weight_used_g, notes }) => {
    const res = await fetch(`${API_BASE}/spools/${id}/use`, {
      method: 'POST',
      headers: apiHeaders,
      body: JSON.stringify({ weight_used_g, notes })
    })
    return res.json()
  },
  archive: async (id) => {
    const res = await fetch(`${API_BASE}/spools/${id}`, {
      method: 'DELETE',
      headers: apiHeaders
    })
    return res.json()
  }
}

const filamentApi = {
  list: async () => {
    const res = await fetch(`${API_BASE}/filaments`, { headers: apiHeaders })
    return res.json()
  }
}

const printersApi = {
  list: async () => {
    const res = await fetch(`${API_BASE}/printers`, { headers: apiHeaders })
    return res.json()
  }
}

function SpoolCard({ spool, onLoad, onUnload, onUse, onArchive }) {
  const [showActions, setShowActions] = useState(false)
  
  const percentRemaining = spool.percent_remaining || 0
  const isLow = percentRemaining < 20
  const isEmpty = spool.status === 'empty'
  const isArchived = spool.status === 'archived'
  
  const statusColor = isEmpty ? 'bg-red-500' : isLow ? 'bg-yellow-500' : 'bg-green-500'
  
  return (
    <div className={clsx(
      "bg-farm-900 rounded-lg p-4 border border-farm-800 hover:border-farm-700 transition-colors",
      isArchived && "opacity-50"
    )}>
      <div className="flex items-start justify-between mb-3">
        <div className="flex items-center gap-3">
          {spool.filament_color_hex && (
            <div 
              className="w-8 h-8 rounded-full border-2 border-farm-700"
              style={{ backgroundColor: `#${spool.filament_color_hex}` }}
            />
          )}
          <div>
            <h3 className="font-medium text-farm-100">
              {spool.filament_brand} {spool.filament_name}
            </h3>
            <p className="text-sm text-farm-400">{spool.filament_material}</p>
          </div>
        </div>
        <span className={clsx(
          "px-2 py-1 rounded text-xs font-medium",
          spool.status === 'active' ? "bg-green-500/20 text-green-400" :
          spool.status === 'empty' ? "bg-red-500/20 text-red-400" :
          "bg-gray-500/20 text-gray-400"
        )}>
          {spool.status}
        </span>
      </div>
      
      {/* Weight bar */}
      <div className="mb-3">
        <div className="flex justify-between text-sm mb-1">
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
      <div className="text-sm text-farm-400 mb-3">
        {spool.location_printer_id ? (
          <span className="flex items-center gap-1">
            <Printer size={14} />
            Printer {spool.location_printer_id}, Slot {spool.location_slot}
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
      <div className="text-xs text-farm-500 mb-3 font-mono">
        {spool.qr_code}
      </div>
      
      {/* Actions */}
      <div className="flex gap-2">
        {spool.location_printer_id ? (
          <button
            onClick={() => onUnload(spool)}
            className="flex-1 px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded text-sm text-farm-200 flex items-center justify-center gap-1"
          >
            <Package size={14} />
            Unload
          </button>
        ) : (
          <button
            onClick={() => onLoad(spool)}
            className="flex-1 px-3 py-1.5 bg-print-600 hover:bg-print-500 rounded text-sm text-white flex items-center justify-center gap-1"
          >
            <Printer size={14} />
            Load
          </button>
        )}
        <a
          href={`${API_BASE}/spools/${spool.id}/label`}
          target="_blank"
          rel="noopener noreferrer"
          className="px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded text-sm text-farm-200 flex items-center justify-center"
        >
          <QrCode size={14} />
        </a>
        <button
          onClick={() => onUse(spool)}
          className="px-3 py-1.5 bg-farm-800 hover:bg-farm-700 rounded text-sm text-farm-200 flex items-center justify-center"
          title="Record usage"
        >
          <Scale size={14} />
        </button>
        {spool.status !== 'archived' && (
          <button
            onClick={() => onArchive(spool)}
            className="px-3 py-1.5 bg-farm-800 hover:bg-red-900 rounded text-sm text-farm-200 hover:text-red-400 flex items-center justify-center"
            title="Archive"
          >
            <Archive size={14} />
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
  
  const handleSubmit = (e) => {
    e.preventDefault()
    onCreate({
      ...form,
      filament_id: parseInt(form.filament_id),
      price: form.price ? parseFloat(form.price) : null
    })
  }
  
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-farm-900 rounded-lg p-6 w-full max-w-md border border-farm-700">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold text-farm-100">Add New Spool</h2>
          <button onClick={onClose} className="text-farm-400 hover:text-farm-200">
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
              className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100"
            >
              <option value="">Select filament...</option>
              {filaments?.map(f => (
                <option key={f.id} value={f.id.replace('lib_', '')}>
                  {f.brand} - {f.name} ({f.material})
                </option>
              ))}
            </select>
          </div>
          
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-farm-400 mb-1">Net Weight (g)</label>
              <input
                type="number"
                value={form.initial_weight_g}
                onChange={(e) => setForm({ ...form, initial_weight_g: parseInt(e.target.value) })}
                className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100"
              />
            </div>
            <div>
              <label className="block text-sm text-farm-400 mb-1">Spool Weight (g)</label>
              <input
                type="number"
                value={form.spool_weight_g}
                onChange={(e) => setForm({ ...form, spool_weight_g: parseInt(e.target.value) })}
                className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100"
              />
            </div>
          </div>
          
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm text-farm-400 mb-1">Vendor</label>
              <input
                type="text"
                value={form.vendor}
                onChange={(e) => setForm({ ...form, vendor: e.target.value })}
                placeholder="Amazon, MatterHackers..."
                className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100"
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
                className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100"
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
              className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100"
            />
          </div>
          
          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded text-farm-200"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="flex-1 px-4 py-2 bg-print-600 hover:bg-print-500 rounded text-white"
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
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-farm-900 rounded-lg p-6 w-full max-w-md border border-farm-700">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold text-farm-100">Load Spool</h2>
          <button onClick={onClose} className="text-farm-400 hover:text-farm-200">
            <X size={20} />
          </button>
        </div>
        
        <div className="mb-4 p-3 bg-farm-800 rounded flex items-center gap-3">
          {spool.filament_color_hex && (
            <div 
              className="w-6 h-6 rounded-full border border-farm-600"
              style={{ backgroundColor: `#${spool.filament_color_hex}` }}
            />
          )}
          <span className="text-farm-200">
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
              className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100"
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
                className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100"
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
              className="flex-1 px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded text-farm-200"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="flex-1 px-4 py-2 bg-print-600 hover:bg-print-500 rounded text-white"
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
  
  const handleSubmit = (e) => {
    e.preventDefault()
    onUse({
      id: spool.id,
      weight_used_g: parseFloat(form.weight_used_g),
      notes: form.notes
    })
  }
  
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-farm-900 rounded-lg p-6 w-full max-w-md border border-farm-700">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold text-farm-100">Record Usage</h2>
          <button onClick={onClose} className="text-farm-400 hover:text-farm-200">
            <X size={20} />
          </button>
        </div>
        
        <div className="mb-4 p-3 bg-farm-800 rounded">
          <div className="flex items-center gap-3 mb-2">
            {spool.filament_color_hex && (
              <div 
                className="w-6 h-6 rounded-full border border-farm-600"
                style={{ backgroundColor: `#${spool.filament_color_hex}` }}
              />
            )}
            <span className="text-farm-200">
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
              className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100"
            />
          </div>
          
          <div>
            <label className="block text-sm text-farm-400 mb-1">Notes (optional)</label>
            <input
              type="text"
              value={form.notes}
              onChange={(e) => setForm({ ...form, notes: e.target.value })}
              placeholder="Print job, waste..."
              className="w-full bg-farm-800 border border-farm-700 rounded px-3 py-2 text-farm-100"
            />
          </div>
          
          <div className="flex gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2 bg-farm-800 hover:bg-farm-700 rounded text-farm-200"
            >
              Cancel
            </button>
            <button
              type="submit"
              className="flex-1 px-4 py-2 bg-print-600 hover:bg-print-500 rounded text-white"
            >
              Record Usage
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

export default function Spools() {
  const queryClient = useQueryClient()
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [loadingSpool, setLoadingSpool] = useState(null)
  const [usingSpool, setUsingSpool] = useState(null)
  const [filter, setFilter] = useState('active')
  
  const { data: spools, isLoading } = useQuery({
    queryKey: ['spools', filter],
    queryFn: () => spoolsApi.list({ status: filter === 'all' ? undefined : filter })
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
    }
  })
  
  const loadMutation = useMutation({
    mutationFn: spoolsApi.load,
    onSuccess: () => {
      queryClient.invalidateQueries(['spools'])
      queryClient.invalidateQueries(['printers'])
      setLoadingSpool(null)
    }
  })
  
  const unloadMutation = useMutation({
    mutationFn: spoolsApi.unload,
    onSuccess: () => {
      queryClient.invalidateQueries(['spools'])
      queryClient.invalidateQueries(['printers'])
    }
  })
  
  const useMutation2 = useMutation({
    mutationFn: spoolsApi.use,
    onSuccess: () => {
      queryClient.invalidateQueries(['spools'])
      setUsingSpool(null)
    }
  })
  
  const archiveMutation = useMutation({
    mutationFn: spoolsApi.archive,
    onSuccess: () => {
      queryClient.invalidateQueries(['spools'])
    }
  })
  
  const handleUnload = (spool) => {
    if (confirm(`Unload ${spool.filament_brand} ${spool.filament_name} from printer?`)) {
      unloadMutation.mutate({ id: spool.id })
    }
  }
  
  const handleArchive = (spool) => {
    if (confirm(`Archive ${spool.filament_brand} ${spool.filament_name}? This will mark it as no longer in use.`)) {
      archiveMutation.mutate(spool.id)
    }
  }
  
  // Summary stats
  const activeSpools = spools?.filter(s => s.status === 'active') || []
  const lowSpools = activeSpools.filter(s => s.percent_remaining < 20)
  const loadedSpools = activeSpools.filter(s => s.location_printer_id)
  
  return (
    <div className="p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-display font-bold text-farm-100">Spools</h1>
          <p className="text-farm-400 mt-1">Track your filament inventory</p>
        </div>
        <button
          onClick={() => setShowCreateModal(true)}
          className="flex items-center gap-2 px-4 py-2 bg-print-600 hover:bg-print-500 rounded-lg text-white"
        >
          <Plus size={20} />
          Add Spool
        </button>
      </div>
      
      {/* Stats */}
      <div className="grid grid-cols-4 gap-4 mb-6">
        <div className="bg-farm-900 rounded-lg p-4 border border-farm-800">
          <div className="text-2xl font-bold text-farm-100">{activeSpools.length}</div>
          <div className="text-sm text-farm-400">Active Spools</div>
        </div>
        <div className="bg-farm-900 rounded-lg p-4 border border-farm-800">
          <div className="text-2xl font-bold text-print-400">{loadedSpools.length}</div>
          <div className="text-sm text-farm-400">Loaded</div>
        </div>
        <div className="bg-farm-900 rounded-lg p-4 border border-farm-800">
          <div className="text-2xl font-bold text-yellow-400">{lowSpools.length}</div>
          <div className="text-sm text-farm-400">Low (&lt;20%)</div>
        </div>
        <div className="bg-farm-900 rounded-lg p-4 border border-farm-800">
          <div className="text-2xl font-bold text-farm-100">
            {activeSpools.reduce((sum, s) => sum + (s.remaining_weight_g || 0), 0).toFixed(0)}g
          </div>
          <div className="text-sm text-farm-400">Total Filament</div>
        </div>
      </div>
      
      {/* Low warning */}
      {lowSpools.length > 0 && (
        <div className="mb-6 p-4 bg-yellow-500/10 border border-yellow-500/30 rounded-lg flex items-center gap-3">
          <AlertTriangle className="text-yellow-500" size={20} />
          <span className="text-yellow-200">
            {lowSpools.length} spool{lowSpools.length > 1 ? 's' : ''} running low on filament
          </span>
        </div>
      )}
      
      {/* Filter tabs */}
      <div className="flex gap-2 mb-6">
        {['active', 'empty', 'archived', 'all'].map(f => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={clsx(
              "px-4 py-2 rounded-lg text-sm font-medium transition-colors",
              filter === f
                ? "bg-print-600 text-white"
                : "bg-farm-800 text-farm-400 hover:bg-farm-700"
            )}
          >
            {f.charAt(0).toUpperCase() + f.slice(1)}
          </button>
        ))}
      </div>
      
      {/* Spool grid */}
      {isLoading ? (
        <div className="text-center text-farm-400 py-12">Loading spools...</div>
      ) : spools?.length === 0 ? (
        <div className="text-center text-farm-400 py-12">
          No spools found. Add your first spool to get started!
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          {spools?.map(spool => (
            <SpoolCard
              key={spool.id}
              spool={spool}
              onLoad={setLoadingSpool}
              onUnload={handleUnload}
              onUse={setUsingSpool}
              onArchive={handleArchive}
            />
          ))}
        </div>
      )}
      
      {/* Modals */}
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
    </div>
  )
}
