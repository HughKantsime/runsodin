import ActiveHms from '../printers/ActiveHms'
import InstalledNozzles from '../printers/InstalledNozzles'
import LivePrintValues, { showPrinterReadings } from '../printers/LivePrintValues'
import { PrintControls } from '../printers/PrintControls'
import PlateCleared from '../printers/PlateCleared'
import { useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import Modal from '../ui/Modal'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { printerTelemetry } from '../../api'
import type { Printer, PrintJob, Job } from '../../types'
import StatusBadge from '../ui/StatusBadge'
import SlotTile, { slotLabel } from './SlotTile'
import { displayName } from '../../utils/displayName'
import { isActivePrintState, isDualNozzle, isH3C, isPrintingState } from '../../utils/shared'
import { number, state, utc } from './data'

export function PrintTelemetry({ job, now = Date.now(), printing = false }: { job?: PrintJob; now?: number; printing?: boolean }) {
  if (!job) return null
  if (job.awaiting_confirmation) return <p role="status">Waiting for print confirmation in Dispatch Recovery.</p>
  const percent = number(job.progress_percent) && job.progress_percent >= 0 && job.progress_percent <= 100 ? job.progress_percent : null
  const remaining = number(job.remaining_minutes) && job.remaining_minutes >= 0 ? job.remaining_minutes : null
  return <div className="f-progress">
    {percent !== null && <div role="progressbar" aria-label="Print progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}><span style={{ width: `${percent}%` }} /></div>}
    <div className="f-row f-mono">
      <span>{percent !== null ? `${percent}%` : 'Progress unknown'}{number(job.current_layer) && number(job.total_layers) ? ` · L ${job.current_layer}/${job.total_layers}` : ''}</span>
      {printing && remaining !== null && <span>Est. done {new Date(now + remaining * 60000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>}
    </div>
  </div>
}
export function Temperatures({ printer }: { printer: Printer }) {
  const dual = isDualNozzle(printer.machine_type)
  const { data } = useQuery({ queryKey: ['nozzle-status', printer.id], queryFn: () => printerTelemetry.nozzleStatus(printer.id) as Promise<{ nozzle_0?: { temp?: number }; nozzle_1?: { temp?: number } }>, enabled: dual, refetchInterval: 30000 })
  return <div className="f-temps f-mono">
    {dual ? <>{number(data?.nozzle_1?.temp) && <span>L nozzle {Math.round(data.nozzle_1.temp)}°</span>}{number(data?.nozzle_0?.temp) && <span>R nozzle {Math.round(data.nozzle_0.temp)}°</span>}</>
      : number(printer.nozzle_temp) && <span>Nozzle {Math.round(printer.nozzle_temp)}°</span>}
    {number(printer.chamber_temp) && <span>Chamber {Math.round(printer.chamber_temp)}°</span>}
    {number(printer.bed_temp) && <span>Bed {Math.round(printer.bed_temp)}°</span>}
  </div>
}
function feed(printer: Printer) {
  if (isH3C(printer)) return ''
  const slots = printer.filament_slots || []
  if (printer.api_type === 'bambu') {
    const units = new Set(slots.filter(s => s.slot_number <= 16).map(s => Math.floor((s.slot_number - 1) / 4))).size
    return [units ? (units === 1 ? 'AMS' : `${units} AMS`) : '', slots.some(s => s.slot_number >= 17 && s.slot_number <= 24) ? 'AMS HT' : ''].filter(Boolean).join(' + ')
  }
  return printer.api_type === 'anycubic_lan' && slots.length ? 'ACE' : ''
}
export default function PrinterCard({ printer, job, next, now, children, selection, statusDetail, slots, rackSlotsVisible = true, slotVariant = 'compact' }: { printer: Printer; job?: PrintJob; next?: Job; now?: number; children?: ReactNode; selection?: ReactNode; statusDetail?: ReactNode; slots?: ReactNode; rackSlotsVisible?: boolean; slotVariant?: 'full' | 'compact' }) {
  const [filamentsOpen, setFilamentsOpen] = useState(false)
  const rack = isH3C(printer)
  return <article className="f-card" aria-label={printer.nickname || printer.name}>
    <div className="f-row"><div className="f-printer-heading">{selection}<div><h2><Link to={`/printers/${printer.id}`}>{printer.nickname || printer.name}</Link></h2><p className="f-secondary">{[printer.model, feed(printer)].filter(Boolean).join(' · ')}</p></div></div><StatusBadge status={state(printer)} /></div>
    {statusDetail}
    {slotVariant === 'compact' && <ActiveHms printer={printer} noticesOnly />}
    {job && <p className="f-job">{displayName(job)}</p>}
    {isActivePrintState(printer.gcode_state) && !job?.awaiting_confirmation && showPrinterReadings(printer, job)
      ? <LivePrintValues printer={printer} foundry />
      : <PrintTelemetry job={job} now={now} printing={isPrintingState(printer.gcode_state)} />}
    {isActivePrintState(printer.gcode_state) && <PrintControls printerId={printer.id} inline />}
    {job?.recording_note && <p>{job.recording_note}</p>}
    {next && <div className="f-next"><p>Next: {displayName(next)}</p>
      {next.hold && <p>Waiting on you · job held</p>}
      {Number.isFinite(utc(next.scheduled_start)) && <p className="f-mono">Scheduled {new Date(utc(next.scheduled_start)).toLocaleString()}</p>}
      {!!next.ams_slot_nozzles?.length && <p className="f-mono">{next.ams_slot_nozzles.map(({ source_index, slot_number, nozzle }) => <span key={source_index}>{nozzle ? `${nozzle === 'right' ? 'Right' : 'Left'} nozzle · ` : ''}{slotLabel(printer.api_type, slot_number)}{source_index < next.ams_slot_nozzles!.length - 1 ? ' · ' : ''}</span>)}</p>}
      {!next.ams_slot_nozzles?.length && next.ams_mapping?.length > 0 && <p className="f-mono">{next.ams_mapping.filter(n => Number.isInteger(n) && n >= 0 && n < 24).map(n => slotLabel('bambu', n + 1)).join(' · ')}</p>}
    </div>}
    <PlateCleared printer={printer} />
    {rack && rackSlotsVisible && slotVariant === 'compact' && <InstalledNozzles rack feedingSlot={isActivePrintState(printer.gcode_state) ? printer.feeding_slot : null} nozzles={printer.installed_nozzles} slots={printer.filament_slots} />}
    {!rack && (slots || printer.filament_slots?.length > 0) && <div className={slotVariant === 'compact' ? 'f-slots f-slots-compact' : 'f-slots'} style={slotVariant === 'compact' ? { gridTemplateColumns: `repeat(${Math.min(5, printer.filament_slots?.length || 4)}, minmax(0, 1fr))` } : undefined}>{slots || printer.filament_slots.map(slot => <SlotTile key={slot.slot_number} apiType={printer.api_type} slot={slot} variant={slotVariant} label={slotVariant === 'compact' && printer.api_type === 'bambu' && slot.slot_number <= 16 && (isDualNozzle(printer.machine_type) || printer.filament_slots.some(s => s.slot_number > 4)) ? `${Math.floor((slot.slot_number - 1) / 4) + 1}·${(slot.slot_number - 1) % 4 + 1}` : undefined} state={printer.feeding_slot === slot.slot_number ? 'feeding' : undefined} />)}</div>}
    {!rack && !slots && slotVariant === 'compact' && printer.filament_slots?.length > 0 && <>
      <button type="button" className="min-h-11 text-left underline" aria-expanded={filamentsOpen}
        onClick={event => { event.stopPropagation(); setFilamentsOpen(true) }}>Filaments</button>
      {filamentsOpen && createPortal(<Modal isOpen title="Filaments" size="xl" onClose={() => setFilamentsOpen(false)}>
        <div className="space-y-2">{printer.filament_slots.map(slot => <SlotTile key={slot.slot_number} slot={slot}
          apiType={printer.api_type} expandable={false} state={printer.feeding_slot === slot.slot_number ? 'feeding' : undefined} />)}</div>
      </Modal>, document.body)}
    </>}
    <Temperatures printer={printer} />
    {children}
  </article>
}
