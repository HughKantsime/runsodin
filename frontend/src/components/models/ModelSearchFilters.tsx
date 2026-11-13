import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { SlidersHorizontal } from 'lucide-react'
import { printers } from '../../api'
import type { ModelLibraryFilters } from '../../api/models'

export const EMPTY_FILTERS: ModelLibraryFilters = {}

export function activeFilterCount(filters: ModelLibraryFilters): number {
  return Object.values(filters).filter(v => v !== undefined && v !== null && v !== '').length
}

const field = 'w-full bg-[var(--brand-input-bg)] border border-[var(--brand-card-border)] rounded-md px-3 py-2 text-sm'
const toNumber = (value: string) => (value.trim() && Number.isInteger(Number(value)) && Number(value) >= 1 ? Number(value) : undefined)

export default function ModelSearchFilters({ value, onChange, resultCount }: { value: ModelLibraryFilters; onChange: (next: ModelLibraryFilters) => void; resultCount: number | null }) {
  const [open, setOpen] = useState(false)
  const [rangeError, setRangeError] = useState('')
  const [draft, setDraft] = useState({
    q: value.q ?? '', material: value.material ?? '', printer_model: value.printer_model ?? '',
    plates_min: value.plates_min?.toString() ?? '', plates_max: value.plates_max?.toString() ?? '',
    printed_on_printer: value.printed_on_printer?.toString() ?? '',
  })
  const { data: printerList } = useQuery({ queryKey: ['printers'], queryFn: () => printers.list(true), enabled: open })
  const count = activeFilterCount(value)
  const set = (key: keyof typeof draft) => (e: { target: { value: string } }) => {
    setDraft(prev => ({ ...prev, [key]: e.target.value }))
    setRangeError('')
  }

  const apply = (e: { preventDefault: () => void }) => {
    e.preventDefault()
    const next: ModelLibraryFilters = {}
    if (draft.q.trim()) next.q = draft.q.trim()
    if (draft.material.trim()) next.material = draft.material.trim()
    if (draft.printer_model.trim()) next.printer_model = draft.printer_model.trim()
    const min = toNumber(draft.plates_min), max = toNumber(draft.plates_max), printer = toNumber(draft.printed_on_printer)
    if (min !== undefined && max !== undefined && min > max) {
      setRangeError('Minimum plates cannot exceed maximum plates.')
      return
    }
    setRangeError('')
    if (min !== undefined) next.plates_min = min
    if (max !== undefined) next.plates_max = max
    if (printer !== undefined) next.printed_on_printer = printer
    onChange(next)
  }
  const clear = () => {
    setRangeError('')
    setDraft({ q: '', material: '', printer_model: '', plates_min: '', plates_max: '', printed_on_printer: '' })
    onChange(EMPTY_FILTERS)
  }

  return (
    <div className="mb-4">
      <button type="button" aria-expanded={open} onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-[var(--brand-card-border)] text-[var(--brand-text-secondary)]">
        <SlidersHorizontal size={14} /> Filters{count > 0 ? ` (${count})` : ''}
      </button>
      {open && (
        <form onSubmit={apply} className="mt-3 p-3 rounded-md border border-[var(--brand-card-border)] bg-[var(--brand-card-bg)] grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3" aria-label="Library filters">
          <label className="text-xs text-[var(--brand-muted)]">Model, file or project name
            <input className={field + ' mt-1'} value={draft.q} maxLength={100} onChange={set('q')} />
          </label>
          <label className="text-xs text-[var(--brand-muted)]">Material
            <input className={field + ' mt-1'} value={draft.material} maxLength={40} placeholder="e.g. PETG" onChange={set('material')} />
          </label>
          <label className="text-xs text-[var(--brand-muted)]">Sliced for printer model
            <input className={field + ' mt-1'} value={draft.printer_model} maxLength={80} placeholder="e.g. A1" onChange={set('printer_model')} />
          </label>
          <label className="text-xs text-[var(--brand-muted)]">Plates, at least
            <input className={field + ' mt-1'} type="number" min="1" value={draft.plates_min} onChange={set('plates_min')} />
          </label>
          <label className="text-xs text-[var(--brand-muted)]">Plates, at most
            <input className={field + ' mt-1'} type="number" min="1" value={draft.plates_max} onChange={set('plates_max')} />
          </label>
          <label className="text-xs text-[var(--brand-muted)]">Printed OK on printer
            <select className={field + ' mt-1'} value={draft.printed_on_printer} onChange={set('printed_on_printer')}>
              <option value="">Any</option>
              {(Array.isArray(printerList) ? printerList : []).map((p: { id: number; name: string }) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
          </label>
          {rangeError && <p role="alert" className="text-sm text-red-400 sm:col-span-2 lg:col-span-3">{rangeError}</p>}
          <div className="flex gap-2 sm:col-span-2 lg:col-span-3">
            <button type="submit" className="px-3 py-1.5 bg-[var(--brand-primary)] rounded-md text-sm">Apply</button>
            <button type="button" onClick={clear} className="px-3 py-1.5 rounded-md text-sm border border-[var(--brand-card-border)]">Clear</button>
          </div>
        </form>
      )}
      {count > 0 && resultCount === 0 && <p role="status" className="mt-2 text-sm text-[var(--brand-muted)]">No models match these filters.</p>}
    </div>
  )
}
