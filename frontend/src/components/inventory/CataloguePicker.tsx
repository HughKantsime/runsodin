import { useId, useState } from 'react'

export default function CataloguePicker({ filaments, value, onChange }: { filaments: any[]; value: string; onChange: (id: string) => void }) {
  const id = useId()
  const [search, setSearch] = useState('')
  const selected = filaments.find(f => String(f.id).replace('lib_', '') === value)
  const visible = filaments.filter(f => f === selected || [f.brand, f.name, f.material, f.catalogue_type, f.color_hex].some(v => String(v || '').toLowerCase().includes(search.toLowerCase())))
  return <div className="min-w-0 space-y-2">
    <label htmlFor={`${id}-search`} className="block text-sm">Search filaments by name, type or colour</label>
    <input id={`${id}-search`} value={search} onChange={e => setSearch(e.target.value)} type="search" className="w-full min-w-0 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md p-2" />
    <label htmlFor={id} className="block text-sm">Filament</label>
    <select id={id} value={value} required onChange={e => onChange(e.target.value)} className="w-full min-w-0 bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md p-2">
      <option value="">Select filament...</option>
      {visible.map(f => <option key={f.id} value={String(f.id).replace('lib_', '')}>{f.brand} - {f.name} ({f.material || 'Material unrecognised'})</option>)}
    </select>
    {selected && <p className="text-sm break-words">{selected.brand} · {selected.name} · {selected.material || 'Material unrecognised'}{selected.color_hex ? ` · #${selected.color_hex}` : ''}</p>}
  </div>
}
