import type { ReactNode } from 'react'
import PrinterNumber from '../ui/PrinterNumber'
import type { FilamentSlot, InstalledNozzle } from '../../types/printer'
import { parseServerDate } from '../../utils/shared'

const rackToolNames: Record<string, string> = { HS01: 'Standard (1.75 mm)', HT01: 'TPU 2.85 mm' }

export function RackFilamentReading({ slot, rawId }: { slot?: FilamentSlot; rawId?: number }) {
  const reading = slot?.rack_filament?.id === rawId ? slot?.rack_filament : null
  const reported = reading && [reading.type, reading.preset,
    reading.color && reading.color !== '00000000' && `#${reading.color.slice(0, 6)}`].filter(Boolean).join(', ')
  return reported ? <div>{reading?.source === 'printer' ? 'Printer reports' : 'Hand-fed at the tool'}: {reported} <small>ID {rawId}</small></div> : null
}

export default function InstalledNozzles({ nozzles, rack = false, slots, renderSlot, feedingSlot }: {
  nozzles?: InstalledNozzle[] | null
  slots?: FilamentSlot[]
  feedingSlot?: number | null
  renderSlot?: (slot: FilamentSlot) => ReactNode
  rack?: boolean
}) {
  if (rack) {
    const tools = new Map(nozzles?.filter(n => n.side === 'rack' && n.position != null).map(n => [n.position!, n]))
    const inventory = new Map(slots?.map(slot => [slot.slot_number, slot]))
    const positions = [...new Set([...tools.keys(), ...inventory.keys()])].sort((a, b) => a - b)
    return <section aria-label="Nozzle rack" className="rounded-lg border border-[var(--brand-card-border)] p-3">
      <h2 className="font-medium">Nozzle rack · Experimental</h2>
      {!tools.size && <p>No rack report received.</p>}
      <ul className="mt-2 grid gap-1 text-sm sm:grid-cols-2">
        {positions.map(position => {
          const nozzle = tools.get(position)
          const slot = inventory.get(position)
          const tool = nozzle?.type && rackToolNames[nozzle.type]
          return <li key={position}>
            {position <= 9 ? 'Position' : 'Slot'} {position}
            {nozzle && <> · {tool ? `${tool} (${nozzle.type})` : nozzle.type || 'Type unknown'} · {nozzle.diameter == null ? 'Diameter unknown' : `${nozzle.diameter} mm`}</>}
            {nozzle?.present === false && ' · Absent'}
            {feedingSlot === position && ' · In use'}
            {nozzle?.mounted && ' · In the tool head'}
            {position <= 9 && <RackFilamentReading slot={slot} rawId={nozzle?.virtual_slot_id ?? 256 - position} />}
            {slot && (renderSlot ? renderSlot(slot) : <div>ODIN slot: {[slot.material_type || slot.filament_type, slot.color,
              slot.color_hex && `#${slot.color_hex.replace(/^#/, '')}`, slot.remaining != null && `${slot.remaining}%`].filter(Boolean).join(', ')}</div>)}
          </li>
        })}
      </ul>
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
