import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { models } from '../../api'
import Button from '../ui/Button'

export default function ModelPlates({ model, onSchedule }) {
  const [open, setOpen] = useState(false)
  const { data, isLoading, error } = useQuery({
    queryKey: ['model-plates', model.id],
    queryFn: () => models.plates(model.id),
    enabled: open,
    staleTime: 0,
  })
  return <details className="mt-3 text-xs" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary className="cursor-pointer">Plates</summary>
    {open && <div className="mt-2 space-y-2">
      {isLoading && <p>Loading plates…</p>}
      {error && <p role="alert">Unable to load plates. Close and reopen to retry.</p>}
      {data?.target_printer_id && <p>Bound to printer {data.target_printer_id}</p>}
      {data?.plates.map(plate => <div key={plate.plate_index} className="flex flex-wrap items-center gap-2">
        <span>Plate {plate.plate_index + 1}</span>
        <span>{plate.print_time_seconds == null ? 'Time unknown' : `${Math.round(plate.print_time_seconds / 60)} min`} · {plate.filament_grams == null ? 'Filament unknown' : `${plate.filament_grams} g`}</span>
        {plate.is_sliced
          ? <Button size="sm" onClick={() => onSchedule({ ...model, selectedPlateIndex: plate.plate_index })}>Schedule plate {plate.plate_index + 1}</Button>
          : <span>Requires slicing</span>}
      </div>)}
      {data && !data.plates.length && <p>No embedded plates.</p>}
    </div>}
  </details>
}
