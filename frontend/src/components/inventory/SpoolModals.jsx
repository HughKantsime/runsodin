import { useState, useEffect } from 'react'
import { spools as spoolsApi } from '../../api'
import { Modal } from '../ui'

const HYGROSCOPIC_TYPES = new Set([
  'PA', 'NYLON_CF', 'NYLON_GF', 'PPS', 'PPS_CF',
  'PETG', 'PETG_CF', 'PC', 'PC_ABS', 'PC_CF', 'TPU', 'PVA',
])

export function CreateSpoolModal({ filaments, onClose, onCreate }) {
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
    <Modal isOpen={true} onClose={onClose} title="Add New Spool" size="md">
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Filament Type</label>
          <select
            value={form.filament_id}
            onChange={(e) => setForm({ ...form, filament_id: e.target.value })}
            required
            className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
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
            <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Net Weight (g)</label>
            <input
              type="number"
              value={form.initial_weight_g}
              onChange={(e) => setForm({ ...form, initial_weight_g: parseInt(e.target.value) })}
              className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
            />
          </div>
          <div>
            <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Spool Weight (g)</label>
            <input
              type="number"
              value={form.spool_weight_g}
              onChange={(e) => setForm({ ...form, spool_weight_g: parseInt(e.target.value) })}
              className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
            />
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Vendor</label>
            <input
              type="text"
              value={form.vendor}
              onChange={(e) => setForm({ ...form, vendor: e.target.value })}
              placeholder="Amazon, MatterHackers..."
              className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
            />
          </div>
          <div>
            <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Price</label>
            <input
              type="number"
              step="0.01"
              value={form.price}
              onChange={(e) => setForm({ ...form, price: e.target.value })}
              placeholder="24.99"
              className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
            />
          </div>
        </div>

        <div>
          <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Storage Location</label>
          <input
            type="text"
            value={form.storage_location}
            onChange={(e) => setForm({ ...form, storage_location: e.target.value })}
            placeholder="Shelf A1, Dry Box 2..."
            className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
          />
        </div>

        <div className="flex gap-3 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="flex-1 px-4 py-2 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md text-[var(--brand-text-secondary)]"
          >
            Cancel
          </button>
          <button
            type="submit"
            className="flex-1 px-4 py-2 bg-[var(--brand-primary)] hover:opacity-90 rounded-md text-white"
          >
            Add Spool
          </button>
        </div>
      </form>
    </Modal>
  )
}

export function LoadSpoolModal({ spool, printers, onClose, onLoad }) {
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
    <Modal isOpen={true} onClose={onClose} title="Load Spool" size="md">
      <div className="mb-4 p-3 bg-[var(--brand-input-bg)] rounded-md flex items-center gap-3">
        {spool.filament_color_hex && (
          <div
            className="w-6 h-6 rounded-full border border-[var(--brand-card-border)] flex-shrink-0"
            style={{ backgroundColor: `#${spool.filament_color_hex}` }}
          />
        )}
        <span className="text-[var(--brand-text-secondary)] truncate">
          {spool.filament_brand} {spool.filament_name}
        </span>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Printer</label>
          <select
            value={form.printer_id}
            onChange={(e) => setForm({ ...form, printer_id: e.target.value, slot_number: '' })}
            required
            className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
          >
            <option value="">Select printer...</option>
            {printers?.map(p => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </div>

        {selectedPrinter && (
          <div>
            <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Slot</label>
            <select
              value={form.slot_number}
              onChange={(e) => setForm({ ...form, slot_number: e.target.value })}
              required
              className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
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
            className="flex-1 px-4 py-2 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md text-[var(--brand-text-secondary)]"
          >
            Cancel
          </button>
          <button
            type="submit"
            className="flex-1 px-4 py-2 bg-[var(--brand-primary)] hover:opacity-90 rounded-md text-white"
          >
            Load
          </button>
        </div>
      </form>
    </Modal>
  )
}

export function UseSpoolModal({ spool, onClose, onUse }) {
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
    <Modal isOpen={true} onClose={onClose} title="Record Usage" size="md">
      <div className="mb-4 p-3 bg-[var(--brand-input-bg)] rounded-md">
        <div className="flex items-center gap-3 mb-2">
          {spool.filament_color_hex && (
            <div
              className="w-6 h-6 rounded-full border border-[var(--brand-card-border)] flex-shrink-0"
              style={{ backgroundColor: `#${spool.filament_color_hex}` }}
            />
          )}
          <span className="text-[var(--brand-text-secondary)] truncate">
            {spool.filament_brand} {spool.filament_name}
          </span>
        </div>
        <div className="text-sm text-[var(--brand-text-muted)]">
          Current: {spool.remaining_weight_g?.toFixed(0)}g remaining
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Weight Used (g)</label>
          <input
            type="number"
            step="0.1"
            value={form.weight_used_g}
            onChange={(e) => setForm({ ...form, weight_used_g: e.target.value })}
            required
            placeholder="50"
            className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
          />
        </div>

        <div>
          <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Notes (optional)</label>
          <input
            type="text"
            value={form.notes}
            onChange={(e) => setForm({ ...form, notes: e.target.value })}
            placeholder="Print job, waste..."
            className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
          />
        </div>

        <div className="flex gap-3 pt-2">
          <button
            type="button"
            onClick={onClose}
            className="flex-1 px-4 py-2 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md text-[var(--brand-text-secondary)]"
          >
            Cancel
          </button>
          <button
            type="submit"
            className="flex-1 px-4 py-2 bg-[var(--brand-primary)] hover:opacity-90 rounded-md text-white"
          >
            Record Usage
          </button>
        </div>
      </form>
    </Modal>
  )
}

export function DryingModal({ spool, onClose, onSubmit }) {
  const [form, setForm] = useState({ duration_hours: '', temp_c: '', method: 'dryer', notes: '' })
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (spool) {
      spoolsApi.dryingHistory(spool.id)
        .then(setHistory)
        .catch(() => {})
        .finally(() => setLoading(false))
    }
  }, [spool])

  const handleSubmit = (e) => {
    e.preventDefault()
    onSubmit({ id: spool.id, ...form, duration_hours: parseFloat(form.duration_hours), temp_c: form.temp_c ? parseFloat(form.temp_c) : null })
  }

  return (
    <Modal isOpen={true} onClose={onClose} title="Log Drying Session" size="md">
      <div className="mb-4 p-3 bg-[var(--brand-input-bg)] rounded-md">
        <div className="flex items-center gap-3">
          {spool.filament_color_hex && (
            <div className="w-6 h-6 rounded-full border border-[var(--brand-card-border)] flex-shrink-0" style={{ backgroundColor: `#${spool.filament_color_hex}` }} />
          )}
          <span className="text-[var(--brand-text-secondary)] truncate">{spool.filament_brand} {spool.filament_name}</span>
          <span className="text-xs text-[var(--brand-text-muted)] ml-auto">{spool.filament_material}</span>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-3">
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Duration (hours) *</label>
            <input type="number" step="0.5" required value={form.duration_hours} onChange={(e) => setForm({ ...form, duration_hours: e.target.value })} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]" placeholder="4" />
          </div>
          <div>
            <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Temperature (°C)</label>
            <input type="number" value={form.temp_c} onChange={(e) => setForm({ ...form, temp_c: e.target.value })} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]" placeholder="55" />
          </div>
        </div>
        <div>
          <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Method</label>
          <select value={form.method} onChange={(e) => setForm({ ...form, method: e.target.value })} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]">
            <option value="dryer">Filament Dryer</option>
            <option value="oven">Oven</option>
            <option value="desiccant">Desiccant Box</option>
          </select>
        </div>
        <div>
          <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Notes</label>
          <input type="text" value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]" placeholder="Before printing nylon parts" />
        </div>
        <div className="flex gap-3 pt-2">
          <button type="button" onClick={onClose} className="flex-1 px-4 py-2 bg-[var(--brand-input-bg)] hover:bg-[var(--brand-card-border)] rounded-md text-[var(--brand-text-secondary)]">Cancel</button>
          <button type="submit" className="flex-1 px-4 py-2 bg-amber-600 hover:bg-amber-500 rounded-md text-white">Log Drying</button>
        </div>
      </form>

      {/* History */}
      {history.length > 0 && (
        <div className="mt-4 border-t border-[var(--brand-card-border)] pt-4">
          <h3 className="text-sm font-medium text-[var(--brand-text-secondary)] mb-2">Drying History</h3>
          <div className="space-y-1.5 max-h-40 overflow-y-auto">
            {history.map(h => (
              <div key={h.id} className="flex items-center justify-between text-xs text-[var(--brand-text-muted)]">
                <span>{h.dried_at ? new Date(h.dried_at).toLocaleDateString() : '—'}</span>
                <span>{h.duration_hours}h @ {h.temp_c ? h.temp_c + '°C' : '—'}</span>
                <span className="capitalize">{h.method}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </Modal>
  )
}

// EditSpoolModal and EditFilamentModal are in SpoolEditModals.jsx
