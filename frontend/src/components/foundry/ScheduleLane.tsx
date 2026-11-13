import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { timeline } from '../../api'
import type { Printer, PrintJob, Job, TimelineSlot } from '../../types'
import { isPausedState, isPrintingState, parseServerDate } from '../../utils/shared'
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
/** Keep one row, with a 44px minimum hit area. Adjacent/overlapping hit areas
 * share a button that opens every job, rather than hiding one beneath another.
 * Undated work occupies an explicitly labelled pocket, never a made-up duration.
 */
export function layoutLane(blocks: LaneBlock[], start: number, hours: number, trackWidth: number) {
  const width = Math.max(44, trackWidth)
  const end = start + hours * 3600000
  const valid = (b: LaneBlock) => number(b.start) && number(b.end) && b.end > b.start!
  const timed = blocks.filter(b => valid(b) && b.end! > start && b.start! < end)
  const unknown = blocks.filter(b => !valid(b))
  const spans = timed.map(block => {
    const left = (Math.max(start, block.start!) - start) / (end - start) * width
    const right = (Math.min(end, block.end!) - start) / (end - start) * width
    return { left: Math.min(left, width - 44), right: Math.min(width, Math.max(right, left + 44)), blocks: [block] }
  })
  if (unknown.length) spans.push({ left: 0, right: Math.min(width, 148), blocks: unknown })
  spans.sort((a, b) => a.left - b.left)
  const groups: typeof spans = []
  for (const span of spans) {
    const last = groups[groups.length - 1]
    if (last && span.left < last.right + 4) {
      last.right = Math.max(last.right, span.right)
      last.blocks.push(...span.blocks)
    } else groups.push({ ...span, blocks: [...span.blocks] })
  }
  return groups.map(group => ({ ...group, left: group.left / width * 100, width: (group.right - group.left) / width * 100, pixels: group.right - group.left }))
}
function blockDescription(block: LaneBlock) {
  const timing = number(block.start) && number(block.end) && block.end > block.start
    ? `${parseServerDate(block.start).toLocaleString()} – ${parseServerDate(block.end).toLocaleString()}` : 'time unknown'
  return `${block.name} · ${block.kind === 'waiting' ? 'waits for you' : block.kind} · ${timing}`
}
export function ScheduleLane({ printer, blocks, start, hours = 12, onSelect }: { printer: Printer; blocks: LaneBlock[]; start: number; hours?: number; onSelect?: (slot: TimelineSlot) => void }) {
  const track = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(300)
  const [selected, setSelected] = useState<LaneBlock[]>([])
  useEffect(() => {
    if (!track.current || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(([entry]) => { if (entry.contentRect.width) setWidth(entry.contentRect.width) })
    observer.observe(track.current)
    return () => observer.disconnect()
  }, [])
  const name = printer.nickname || printer.name
  return <div className="f-lane"><Link className="f-lane-name" title={name} to={`/printers/${printer.id}`}>{name}</Link>
    <div ref={track} className="f-lane-track">{layoutLane(blocks, start, hours, width).map(group => {
      const block = group.blocks[0]
      const kind = group.blocks.some(b => b.kind === 'waiting') ? 'waiting' : block.kind
      const description = group.blocks.map(blockDescription).join('\n')
      const label = group.blocks.length > 1 ? `${group.blocks.length} jobs` : kind === 'waiting' ? 'Waits for you'
        : kind === 'printing' ? 'Print' : 'Queue'
      return <button type="button" key={group.blocks.map(b => b.key).join('-')} className="f-lane-block" data-kind={kind}
        title={description} aria-label={description} style={{ left: `${group.left}%`, width: `${group.width}%` }}
        onClick={() => {
          if (group.blocks.length === 1 && block.slot && onSelect) onSelect(block.slot)
          else setSelected(group.blocks)
        }}><LaneLabel label={label} /></button>
    })}</div>
    {selected.length > 0 && createPortal(<DetailDrawer open onClose={() => setSelected([])} title={`${name} · schedule`}>
      <ul className="f-lane-details">{selected.map(block => <li key={block.key}><p>{blockDescription(block)}</p>
        {block.slot && onSelect && <button type="button" className="f-button" onClick={() => { setSelected([]); onSelect(block.slot!) }}>Open job details</button>}
      </li>)}</ul>
    </DetailDrawer>, document.body)}
  </div>
}
/** Measure the actual font, including after webfonts load. Never split or clip a
 * short label; the button's accessible name always carries the complete facts. */
function LaneLabel({ label }: { label: string }) {
  const ref = useRef<HTMLSpanElement>(null)
  const [fits, setFits] = useState(false)
  useEffect(() => {
    const element = ref.current
    const button = element?.parentElement
    if (!element || !button) return
    let active = true
    const measure = () => {
      if (!active) return
      const style = getComputedStyle(button)
      setFits(element.scrollWidth <= button.clientWidth - (parseFloat(style.paddingLeft) || 0) - (parseFloat(style.paddingRight) || 0))
    }
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    observer?.observe(button)
    observer?.observe(element)
    void document.fonts?.ready.then(measure)
    measure()
    return () => { active = false; observer?.disconnect() }
  }, [label])
  return <span ref={ref} aria-hidden="true" style={{ visibility: fits ? 'visible' : 'hidden' }}>{label}</span>
}
export function Schedule({ printers, prints = [], queue = [], now }: { printers: Printer[]; prints?: PrintJob[]; queue?: Job[]; now: number }) {
  const [selected, setSelected] = useState<TimelineSlot | null>(null)
  // One query per minute, shared with the Floor; stable across rerenders.
  const start = Math.floor(now / 60000) * 60000
  const query = useQuery({ queryKey: ['timeline', 'foundry', start], queryFn: () => timeline.get(new Date(start).toISOString(), 1), refetchInterval: 30000 })
  return <section className="f-schedule" aria-label="Next 12 hours"><div className="f-row"><h2>Next 12 hours</h2><Link to="/jobs">Jobs</Link></div>
    {query.isError ? <p role="alert">Could not load schedule. <button onClick={() => query.refetch()}>Retry</button></p> : query.isLoading ? <p role="status">Loading schedule…</p> : <>
      <div className="f-schedule-scroll"><div className="f-time-axis f-mono"><span /><div>{[0, 3, 6, 9, 12].map(hour => <time key={hour} data-hour={hour} dateTime={new Date(start + hour * 3600000).toISOString()}>{new Date(start + hour * 3600000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })}</time>)}</div></div>
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
