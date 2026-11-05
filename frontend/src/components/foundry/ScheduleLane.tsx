import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { timeline } from '../../api'
import type { Printer, PrintJob, Job, TimelineSlot } from '../../types'
import { isPausedState, isPrintingState } from '../../utils/shared'
import { displayName } from '../../utils/displayName'
import { number, utc, useNow, useFloorData } from './data'
import DetailDrawer from '../shared/DetailDrawer'
import BlockDetail from '../jobs/TimelineJobDetail'

export type LaneBlock = { key: string; name: string; start?: number; end?: number; kind: 'printing' | 'queued' | 'waiting'; slot?: TimelineSlot }
export function laneBlocks(printer: Printer, slots: TimelineSlot[], prints: PrintJob[], queue: Job[], now: number): LaneBlock[] {
  const blocks: LaneBlock[] = []
  const live = prints.filter(j => j.printer_id === printer.id && j.status === 'running').sort((a, b) => b.id - a.id)[0]
  for (const slot of slots.filter(s => s.printer_id === printer.id && !s.is_setup && ['printing', 'scheduled', 'pending', 'paused'].includes(s.status || ''))) {
    if (live && (slot.mqtt_job_id === live.id || slot.job_id != null && slot.job_id === live.scheduled_job_id)) continue
    const held = queue.find(j => j.id === slot.job_id)?.hold
    blocks.push({ key: `slot-${slot.job_id}-${slot.mqtt_job_id}-${slot.start}`, name: displayName(slot), start: utc(slot.start), end: utc(slot.end), kind: held || slot.status === 'paused' ? 'waiting' : slot.status === 'printing' ? 'printing' : 'queued', slot })
  }
  if (live && (isPrintingState(printer.gcode_state) || isPausedState(printer.gcode_state))) {
    const waiting = isPausedState(printer.gcode_state)
    // Pauses have no known finish. Do not give the waiting period a made-up width.
    blocks.push({ key: `live-${live.id}`, name: displayName(live), kind: waiting ? 'waiting' : 'printing', start: now,
      end: !waiting && number(live.remaining_minutes) && live.remaining_minutes > 0 ? now + live.remaining_minutes * 60000 : undefined })
  }
  for (const job of queue.filter(j => (j.printer_id ?? j.pinned_printer_id) === printer.id && ['pending', 'scheduled'].includes(j.status))) {
    if (slots.some(s => s.job_id === job.id)) continue
    blocks.push({ key: `queue-${job.id}`, name: displayName(job), kind: job.hold ? 'waiting' : 'queued', start: utc(job.scheduled_start), end: utc(job.scheduled_end) })
  }
  return blocks
}
export function ScheduleLane({ printer, blocks, start, hours = 12, onSelect }: { printer: Printer; blocks: LaneBlock[]; start: number; hours?: number; onSelect?: (slot: TimelineSlot) => void }) {
  const end = start + hours * 3600000
  const timed = blocks.filter(b => number(b.start) && number(b.end) && b.end > b.start! && b.end > start && b.start! < end)
  const unknown = blocks.filter(b => !number(b.start) || !number(b.end) || b.end <= b.start!)
  return <div className="f-lane"><Link className="f-lane-name" to={`/printers/${printer.id}`}>{printer.nickname || printer.name}</Link><div>
    <div className="f-lane-track" style={{ minHeight: `${Math.max(1, timed.length) * 48}px` }}>{timed.map((block, index) => {
      const left = (Math.max(start, block.start!) - start) / (end - start) * 100
      const width = (Math.min(end, block.end!) - Math.max(start, block.start!)) / (end - start) * 100
      const label = `${block.name} · ${block.kind === 'waiting' ? 'waits for you' : block.kind}`
      return <button type="button" key={block.key} className="f-lane-block" data-kind={block.kind} title={`${label} · ${new Date(block.start!).toLocaleString()} – ${new Date(block.end!).toLocaleString()}`} style={{ left: `${left}%`, width: `${width}%`, top: index * 48 }} onClick={() => block.slot && onSelect?.(block.slot)} disabled={!block.slot || !onSelect}>{label}</button>
    })}</div>
    {unknown.map(block => <p className="f-undated" data-kind={block.kind} key={block.key}>{block.name} · {block.kind === 'waiting' ? 'waits for you' : block.kind} · time unknown</p>)}
  </div></div>
}
export function Schedule({ printers, prints = [], queue = [], now }: { printers: Printer[]; prints?: PrintJob[]; queue?: Job[]; now: number }) {
  const [selected, setSelected] = useState<TimelineSlot | null>(null)
  // One query per minute, shared with the Floor; stable across rerenders.
  const start = Math.floor(now / 60000) * 60000
  const query = useQuery({ queryKey: ['timeline', 'foundry', start], queryFn: () => timeline.get(new Date(start).toISOString(), 1), refetchInterval: 30000 })
  return <section className="f-schedule" aria-label="Next 12 hours"><div className="f-row"><h2>Next 12 hours</h2><Link to="/jobs">Jobs</Link></div>
    {query.isError ? <p role="alert">Could not load schedule. <button onClick={() => query.refetch()}>Retry</button></p> : query.isLoading ? <p role="status">Loading schedule…</p> : <>
      <div className="f-schedule-scroll"><div className="f-time-axis f-mono"><span /><div>{[0, 3, 6, 9, 12].map(hour => <time key={hour} dateTime={new Date(start + hour * 3600000).toISOString()}>{new Date(start + hour * 3600000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time>)}</div></div>
        {printers.map(printer => <ScheduleLane key={printer.id} printer={printer} blocks={laneBlocks(printer, query.data?.slots || [], prints, queue, now)} start={start} onSelect={setSelected} />)}
      </div>{!printers.length && <p>No printers configured.</p>}
    </>}
    <p className="f-legend">Solid: printing · violet dashed: queued · amber dashed: waits for you</p>
    <DetailDrawer open={!!selected} onClose={() => setSelected(null)} title={selected ? displayName(selected) : 'Job details'}>{selected && <BlockDetail block={selected} />}</DetailDrawer>
  </section>
}
export function FoundryTimeline() {
  const { fleet, prints, queue } = useFloorData()
  const now = useNow()
  return <div className="f-page"><div className="f-row"><h1>Timeline</h1><Link className="f-primary" to="/jobs">Schedule a print</Link></div><Schedule printers={fleet.data || []} prints={prints.data} queue={queue.data} now={now} /></div>
}
