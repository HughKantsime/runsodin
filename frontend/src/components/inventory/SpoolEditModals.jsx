import { useState, useEffect } from 'react'
import { Modal } from '../ui'

export function EditSpoolModal({ spool, onClose, onSave }) {
  const [form, setForm] = useState({
    remaining_weight_g: spool?.remaining_weight_g || 0,
    notes: spool?.notes || '',
    storage_location: spool?.storage_location || '',
  });

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
    <Modal isOpen={true} onClose={onClose} title="Edit Spool" size="md">
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
    </Modal>
  );
}

export function EditFilamentModal({ filament, onClose, onSave }) {
  const [form, setForm] = useState({
    brand: filament?.brand || '',
    name: filament?.name || '',
    material: filament?.material || 'PLA',
    color_hex: filament?.color_hex || '',
    cost_per_gram: filament?.cost_per_gram || ''
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    onSave({ id: filament?.id, ...form })
  }

  return (
    <Modal isOpen={true} onClose={onClose} title={filament ? 'Edit Filament' : 'Add Filament'} size="md">
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
    </Modal>
  )
}
