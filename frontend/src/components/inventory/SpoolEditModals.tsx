import { useState, useEffect } from 'react'
import { Modal } from '../ui'

export function EditSpoolModal({ spool, onClose, onSave }: { spool: any; onClose: () => void; onSave: (data: any) => void }) {
  const [form, setForm] = useState({
    remaining_weight_g: spool?.remaining_weight_g ?? 0,
    pa_profile: spool?.pa_profile ?? '',
    low_stock_threshold_g: spool?.low_stock_threshold_g ?? 50,
    notes: spool?.notes || '',
    storage_location: spool?.storage_location || '',
    spoolman_spool_id: spool?.spoolman_spool_id ?? '',
  });

  const [error, setError] = useState('');

  const handleSubmit = (e) => {
    e.preventDefault();
    const threshold = Number(form.low_stock_threshold_g);
    if (String(form.low_stock_threshold_g).trim() === '' || !Number.isSafeInteger(threshold) || threshold < 0 || threshold > 2_147_483_647) {
      setError('Low-stock threshold must be a whole number from 0 to 2147483647.');
      return;
    }
    const spoolmanText = String(form.spoolman_spool_id).trim();
    const spoolmanId = spoolmanText === '' ? null : Number(spoolmanText);
    if (spoolmanId !== null && (!Number.isSafeInteger(spoolmanId) || spoolmanId < 1)) {
      setError('Spoolman spool ID must be a positive whole number, or blank to unlink.');
      return;
    }
    onSave({
      id: spool.id,
      spoolman_spool_id: spoolmanId,
      pa_profile: form.pa_profile || null,
      low_stock_threshold_g: threshold,
      remaining_weight_g: parseFloat(form.remaining_weight_g),
      notes: form.notes || null,
      storage_location: form.storage_location || null,
    });
  };

  if (!spool) return null;

  return (
    <Modal isOpen={true} onClose={onClose} title="Edit Spool" size="md">
      <div className="mb-4 p-3 bg-[var(--brand-input-bg)] rounded-md">
        <div className="flex items-center gap-3 mb-2">
          {spool.filament_color_hex && (
            <div
              className="w-8 h-8 rounded-full border border-[var(--brand-card-border)] shrink-0"
              style={{ backgroundColor: '#' + spool.filament_color_hex }}
            />
          )}
          <div>
            <div className="text-[var(--brand-text-secondary)] font-medium">
              {spool.filament_brand} {spool.filament_name}
            </div>
            <div className="text-xs text-[var(--brand-text-muted)] font-mono">{spool.qr_code}</div>
            {spool.pa_profile && <div className="text-xs text-[var(--brand-text-muted)]">Stored PA profile: <span>{spool.pa_profile}</span></div>}
          </div>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        {error && <p role="alert" className="text-red-400 text-sm">{error}</p>}
        <div>
          <label htmlFor="spool-pa-profile" className="block text-sm text-[var(--brand-text-muted)] mb-1">PA Profile</label>
          <input id="spool-pa-profile" type="text" maxLength={50} value={form.pa_profile}
            onChange={e => setForm({ ...form, pa_profile: e.target.value })}
            className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]" />
        </div>
        <div>
          <label htmlFor="spool-spoolman-id" className="block text-sm text-[var(--brand-text-muted)] mb-1">Spoolman spool ID</label>
          <input id="spool-spoolman-id" type="number" min="1" step="1" value={form.spoolman_spool_id}
            onChange={e => setForm({ ...form, spoolman_spool_id: e.target.value })}
            placeholder="Blank = not linked"
            className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]" />
        </div>
        <div>
          <label htmlFor="spool-low-stock" className="block text-sm text-[var(--brand-text-muted)] mb-1">Low-stock threshold (g)</label>
          <input id="spool-low-stock" type="number" min="0" max="2147483647" step="1" required value={form.low_stock_threshold_g}
            onChange={e => setForm({ ...form, low_stock_threshold_g: e.target.value })}
            className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]" />
        </div>
        <div>
          <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Remaining Weight (g)</label>
          <input
            type="number"
            step="1"
            min="0"
            value={form.remaining_weight_g}
            onChange={(e) => setForm({ ...form, remaining_weight_g: e.target.value })}
            required
            className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
          />
          <div className="text-xs text-[var(--brand-text-muted)] mt-1">
            Initial: {spool.initial_weight_g}g
          </div>
        </div>

        <div>
          <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Storage Location</label>
          <input
            type="text"
            value={form.storage_location}
            onChange={(e) => setForm({ ...form, storage_location: e.target.value })}
            placeholder="Shelf A, Drawer 2..."
            className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
          />
        </div>

        <div>
          <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Notes</label>
          <input
            type="text"
            value={form.notes}
            onChange={(e) => setForm({ ...form, notes: e.target.value })}
            placeholder="Any notes..."
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
            Save
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function EditFilamentModal({ filament, onClose, onSave }: { filament: any; onClose: () => void; onSave: (data: any) => void }) {
  const [form, setForm] = useState({
    brand: filament?.brand || '',
    name: filament?.name || '',
    material: filament?.material || 'PLA',
    color_hex: filament?.color_hex || '',
    cost_per_gram: filament?.cost_per_gram ?? ''
  })

  const handleSubmit = (e) => {
    e.preventDefault()
    // The API takes a number or null (blank = use the global default rate).
    const cost = form.cost_per_gram === '' ? null : Number(form.cost_per_gram)
    onSave({ id: filament?.id, ...form, cost_per_gram: cost })
  }

  return (
    <Modal isOpen={true} onClose={onClose} title={filament ? 'Edit Filament' : 'Add Filament'} size="md">
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Brand</label>
          <input
            type="text"
            value={form.brand}
            onChange={(e) => setForm({ ...form, brand: e.target.value })}
            required
            placeholder="Bambu Lab, Polymaker, eSUN..."
            className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
          />
        </div>

        <div>
          <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Name</label>
          <input
            type="text"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            required
            placeholder="Matte Black, Silk Gold..."
            className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
          />
        </div>

        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Material</label>
            <select
              value={form.material}
              onChange={(e) => setForm({ ...form, material: e.target.value })}
              className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
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
            <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Color Hex</label>
            <div className="flex gap-2">
              <input
                type="text"
                value={form.color_hex}
                onChange={(e) => setForm({ ...form, color_hex: e.target.value.replace('#', '') })}
                placeholder="FF5733"
                maxLength={6}
                className="flex-1 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)] font-mono"
              />
              {form.color_hex && form.color_hex.length >= 3 && (
                <div
                  className="w-10 h-10 rounded-md border border-[var(--brand-card-border)] shrink-0"
                  style={{ backgroundColor: `#${form.color_hex}` }}
                />
              )}
            </div>
          </div>
        </div>

        <div>
          <label className="block text-sm text-[var(--brand-text-muted)] mb-1">Cost per Gram ($/g)</label>
          <input
            type="number"
            step="0.001"
            min="0"
            value={form.cost_per_gram}
            onChange={(e) => setForm({ ...form, cost_per_gram: e.target.value })}
            placeholder="0.025 (leave blank for global default)"
            className="w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-[var(--brand-text-primary)]"
          />
          <p className="text-xs text-[var(--brand-text-muted)] mt-1">Used for cost calculation. Leave blank to use system default.</p>
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
            {filament ? 'Save Changes' : 'Add Filament'}
          </button>
        </div>
      </form>
    </Modal>
  )
}
