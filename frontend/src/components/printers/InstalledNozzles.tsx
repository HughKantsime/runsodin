import PrinterNumber from '../ui/PrinterNumber'
import type { FilamentSlot, InstalledNozzle } from '../../types/printer'
import { parseServerDate } from '../../utils/shared'

const rackToolNames: Record<string, string> = { HS01: 'Standard (1.75 mm)', HT01: 'TPU 2.85 mm' }

export function RackFilamentReading({ slot, rawId }: { slot?: FilamentSlot; rawId?: number }) {
  const reading = slot?.rack_filament?.id === rawId ? slot?.rack_filament : null
  const reported = reading && [reading.type, reading.preset,
    reading.color && reading.color !== '00000000' && `#${reading.color.slice(0, 6)}`].filter(Boolean).join(', ')
  return <div>Printer reports: {reported || 'Nothing reported'} {rawId != null && <small>ID {rawId}</small>}</div>
}

export default function InstalledNozzles({ nozzles, rack = false, slots, showOperatorSlots = true }: {
  nozzles?: InstalledNozzle[] | null
  slots?: FilamentSlot[]
  showOperatorSlots?: boolean
  rack?: boolean
}) {
  if (rack) {
    const positions = nozzles?.filter(nozzle => nozzle.side === 'rack')
    return <section aria-label="Nozzle rack" className="rounded-lg border border-[var(--brand-card-border)] p-3">
      <h2 className="font-medium">Nozzle rack · Experimental</h2>
      {positions?.length ? <ul className="mt-2 grid gap-1 text-sm sm:grid-cols-2">
        {positions.map(nozzle => {
          const slot = slots?.find(slot => slot.slot_number === nozzle.position)
          const tool = nozzle.type && rackToolNames[nozzle.type]
          return <li key={nozzle.nozzle_id}>
            Position {nozzle.position} · {tool ? `${tool} (${nozzle.type})` : nozzle.type || 'Type unknown'} · {nozzle.diameter == null ? 'Diameter unknown' : `${nozzle.diameter} mm`}
            {nozzle.present === false && ' · Absent'}
            {nozzle.mounted && ' · In the tool head'}
            <RackFilamentReading slot={slot} rawId={nozzle.virtual_slot_id} />
            {showOperatorSlots && slot && <div>ODIN slot: {[slot.filament_type, slot.color, slot.color_hex && `#${slot.color_hex.replace(/^#/, '')}`].filter(Boolean).join(', ')}</div>}
          </li>
        })}
      </ul> : <p>No rack report received.</p>}
    </section>
  }
  return <section aria-label="Installed nozzles" className="mb-6 rounded-lg border border-[var(--brand-card-border)] p-4">
    <h2 className="font-medium">Installed nozzles</h2>
    {!nozzles?.length ? <p>No nozzle report received. Nozzle compatibility has not been verified.</p> :
      <ul className="mt-2 space-y-3">{nozzles.map(nozzle => <li key={nozzle.extruder}>
        <p className="capitalize">{nozzle.side} nozzle{nozzle.present === false ? ' · absent' : ''}</p>
        <PrinterNumber as="p">{nozzle.diameter == null ? 'Diameter unknown' : `${nozzle.diameter} mm`} · {nozzle.flow || 'Flow unknown'} · {nozzle.material || 'Material unknown'}</PrinterNumber>
        <PrinterNumber as="p">Wear: {nozzle.wear == null ? 'Unknown' : nozzle.wear}</PrinterNumber>
        <p className="text-xs">Last reported: <time dateTime={nozzle.observed_at}>{parseServerDate(nozzle.observed_at).toLocaleString()}</time></p>
      </li>)}</ul>}
    <p className="mt-2 text-xs">Last reported values; dispatch checks a fresh printer report.</p>
  </section>
}
