import { useQuery } from '@tanstack/react-query'
import { fetchAPI } from '../../api/client'

type Slice = {
  plates: Array<{ plate: string; estimated_seconds: string; filaments: Array<{ source_slot: string; material: string; color: string }> }>
  selected_plate_index: number
  slots: Array<{ slot_number: number; filament_type: string; color_hex: string | null; loaded_at: string | null }>
  freshness: string
}

export default function SubmissionSliceInfo({ id, revision, printerId, orgId, audience = '' }: { id: number; revision: number; printerId?: number; orgId?: number | null; audience?: string }) {
  const params = new URLSearchParams()
  if (printerId) params.set('printer_id', String(printerId))
  if (orgId) params.set('org_id', String(orgId))
  const query = useQuery({ queryKey: ['education-slice-info', audience, id, revision, printerId, orgId],
    queryFn: () => fetchAPI<Slice>(`/education/submissions/${id}/slice-info${params.size ? `?${params}` : ''}`) })
  if (query.isLoading) return <p>Reading verified slice metadata…</p>
  if (!query.data) return <p role="status">Slice metadata unavailable: {query.error?.message}</p>
  const data = query.data
  const plate = data.plates.find(p => Number(p.plate) === data.selected_plate_index + 1)
  const mismatch = plate?.filaments.some(f => {
    const slot = data.slots.find(s => s.slot_number === Number(f.source_slot))
    return slot && slot.filament_type && slot.filament_type.toUpperCase() !== f.material?.toUpperCase()
  })
  return <section className="my-2 text-xs" aria-label="Slice and loaded filament information">
    {data.plates.map(p => <div key={p.plate}>
      <strong>Plate {p.plate}</strong> · Estimated {p.estimated_seconds ? `${Math.round(Number(p.estimated_seconds) / 60)} min` : 'time unavailable'}
      {p.filaments.map(f => <p key={f.source_slot}>Sliced {f.material || 'unknown material'} · {f.color || 'unknown color'} · source slot {f.source_slot}</p>)}
    </div>)}
    {printerId && <div><strong>Loaded printer slots</strong><p>{data.freshness}</p>
      {data.slots.map(s => <p key={s.slot_number}>Slot {s.slot_number}: {s.filament_type} · {s.color_hex || 'unknown color'} · observed {s.loaded_at || 'time unknown'}</p>)}
      {!data.slots.length && <p>No recorded slots.</p>}
    </div>}
    {mismatch && <p role="alert" className="text-[var(--status-warning)]">Filament mismatch: sliced material differs from the printer’s corresponding loaded slot. Confirm the mapping before approving. Dispatch checks still apply.</p>}
  </section>
}
