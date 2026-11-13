import { useQuery } from '@tanstack/react-query'
import { filamentCheck, type FilamentSlotStatus } from '../../api/filamentCheck'

const STATUS_LABEL: Record<FilamentSlotStatus, string> = { enough: 'OK', short: 'Short', unknown: 'Not checked' }

export default function FilamentCheckPanel({ jobId, printerId }: { jobId: number; printerId: number }) {
  const query = useQuery({
    queryKey: ['filament-check', jobId, printerId],
    queryFn: () => filamentCheck.get(jobId, printerId),
    retry: false,
    staleTime: 0,
  })
  if (query.isLoading) return <p role="status" className="text-xs">Checking filament...</p>
  if (query.isError) return <p role="status" className="text-xs">Filament check unavailable. Dispatch is not blocked by this display.</p>
  const check = query.data
  if (!check || check.overall === 'off') return null
  const heading = check.overall === 'short'
    ? (check.would_hold ? 'Filament short: dispatch will be refused' : 'Filament may run out: dispatch will go ahead with a warning')
    : check.overall === 'enough' ? 'Filament looks sufficient' : 'Filament could not be fully checked'
  return <section role="status" aria-label="Filament check" className="space-y-1 text-sm break-words border border-[var(--brand-card-border)] rounded p-3 mt-2">
    <strong>{heading}</strong>
    {check.slots.length > 0 && <ul className="text-xs space-y-1">
      {check.slots.map((slot, index) => <li key={`${slot.slot ?? 'none'}-${index}`} data-status={slot.status}>
        <span className="font-medium">{STATUS_LABEL[slot.status]}:</span> {slot.message}
      </li>)}
    </ul>}
  </section>
}
