import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { timeline } from '../../api'
import type { Printer, PrintJob, Job, TimelineSlot } from '../../types'
import { isPausedState, isPrintingState, parseServerDate } from '../../utils/shared'
import { displayName } from '../../utils/displayName'
import { number, utc, useNow, useFloorData, state } from './data'
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
    const start = utc(job.scheduled_start), end = utc(job.scheduled_end)
    const slot: TimelineSlot | undefined = number(start) && number(end) && end > start ? {
      start: job.scheduled_start!, end: job.scheduled_end!, printer_id: printer.id, printer_name: printer.nickname || printer.name,
      job_id: job.id, item_name: job.item_name, display_name: job.display_name, status: job.status, mqtt_job_id: null, is_setup: false, colors: job.colors_list,
    } : undefined
    blocks.push({ key: `queue-${job.id}`, name: displayName(job), kind: job.hold ? 'waiting' : 'queued', start, end, slot })
  }
  return blocks
}
const hasTime = (b: LaneBlock) => number(b.start) && number(b.end) && b.end > b.start!
/** Pointer reach belongs to the lane; keyboard buttons keep their true geometry. */
export function layoutLane(blocks: LaneBlock[], start: number, hours: number, trackWidth: number) {
  const width = Math.max(44, trackWidth)
  const end = start + hours * 3600000
  const timed = blocks.filter(b => hasTime(b) && b.end! > start && b.start! < end)
  const unknown = blocks.filter(b => !hasTime(b))
  const groups = timed.map(block => ({
    left: (Math.max(start, block.start!) - start) / (end - start) * width,
    right: (Math.min(end, block.end!) - start) / (end - start) * width,
    blocks: [block], unknown: false,
  })).sort((a, b) => a.left - b.left)
  // The undated pocket has its own column before the shared time origin.
  if (unknown.length) groups.unshift({ left: 0, right: 148, unknown: true, blocks: unknown })
  return groups.map(group => ({ ...group, row: 0, left: group.left / width * 100,
    width: (group.right - group.left) / width * 100, pixels: group.right - group.left }))
}
/** Distances are measured from the drawn interval, with time order breaking ties. */
export function jobsWithinReach(groups: ReturnType<typeof layoutLane>, x: number, trackWidth: number): LaneBlock[] {
  return groups.filter(group => !group.unknown).map(group => {
    const left = group.left / 100 * Math.max(44, trackWidth)
    return { group, distance: Math.max(left - x, x - left - group.pixels, 0) }
  }).filter(({ distance }) => distance <= 22).sort((a, b) => a.distance - b.distance)
    .flatMap(({ group }) => group.blocks)
}
function blockDescription(block: LaneBlock) {
  const timing = number(block.start) && number(block.end) && block.end > block.start
    ? `${parseServerDate(block.start).toLocaleString()} – ${parseServerDate(block.end).toLocaleString()}` : 'Time not set'
  return `${block.name} · ${block.kind === 'waiting' ? 'waits for you' : block.kind} · ${timing}`
}
export function ScheduleLane({ printer, blocks, start, hours = 12, onSelect }: { printer: Printer; blocks: LaneBlock[]; start: number; hours?: number; onSelect?: (slot: TimelineSlot) => void }) {
  const track = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(300)
  const [selected, setSelected] = useState<LaneBlock[]>([])
  const [reachable, setReachable] = useState(false)
  useEffect(() => {
    if (!track.current || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(([entry]) => { if (entry.contentRect.width) setWidth(entry.contentRect.width) })
    observer.observe(track.current)
    return () => observer.disconnect()
  }, [])
  const name = printer.nickname || printer.name
  const groups = layoutLane(blocks, start, hours, width)
  const status = [printer.maintenance?.state === 'hold' ? 'Maintenance hold' : '', state(printer) === 'offline' ? 'Offline' : ''].filter(Boolean).join(' · ')
  const offset = status ? 24 : 0
  const open = (jobs: LaneBlock[]) => {
    if (jobs.length === 1 && jobs[0].slot && onSelect) onSelect(jobs[0].slot)
    else if (jobs.length) setSelected(jobs)
  }
  const atPointer = (clientX: number) => jobsWithinReach(groups, clientX - track.current!.getBoundingClientRect().left, width)
  const renderTarget = (group: ReturnType<typeof layoutLane>[number]) => {
    const block = group.blocks[0]
    const description = `${group.blocks.length > 1 ? `${group.blocks.length} jobs · ` : ''}${group.blocks.map(blockDescription).join('\n')}`
    return <button type="button" key={group.blocks.map(b => b.key).join('-')} className={group.unknown ? 'f-lane-hit f-lane-unknown' : 'f-lane-block'}
      data-kind={group.unknown ? undefined : block.kind}
      title={description} aria-label={description}
      style={{ left: group.unknown ? 0 : `${group.left}%`, width: group.unknown ? 'var(--unknown-width)' : `${group.width}%`, top: offset + 2, '--bar-pixels': `${group.pixels}px` } as CSSProperties}
      onClick={event => {
        // Native keyboard/assistive activation has no pointer click count.
        if (group.unknown || event.detail === 0) { event.stopPropagation(); open(group.blocks) }
      }}>
      {group.unknown ? <span className="f-lane-pocket f-time-unset">
        <span className="f-lane-pocket-size" aria-hidden="true">{group.blocks.length > 1 ? `${group.blocks.length} jobs · ` : ''}Time not set</span>
        {group.blocks.length > 1 && <span className="f-lane-pocket-count">{group.blocks.length} jobs · </span>}Time not set</span>
        : <LaneLabel label={group.blocks.length > 1 ? `${group.blocks.length} jobs` : block.name} />}
    </button>
  }
  return <div className="f-lane" style={groups.some(g => g.unknown) ? { '--unknown-width': 'var(--schedule-unknown-width, 148px)' } as CSSProperties : undefined}><Link className="f-lane-name" title={name} to={`/printers/${printer.id}`}>{name}</Link>
    <div className="f-lane-track" style={{ height: offset + 52 }}>
      {status && <span className="f-lane-status">{status}</span>}
      {groups.filter(g => g.unknown).map(renderTarget)}
      <div ref={track} className="f-lane-timed" style={{ cursor: reachable ? 'pointer' : 'default' }}
        onClick={event => open(atPointer(event.clientX))}
        onPointerMove={event => setReachable(atPointer(event.clientX).length > 0)}
        onPointerLeave={() => setReachable(false)}>{groups.filter(g => !g.unknown).map(renderTarget)}</div>
    </div>
    {selected.length > 0 && createPortal(<DetailDrawer open onClose={() => setSelected([])} title={`${name} · schedule`}>
      <ul className="f-lane-details">{selected.map(block => <li key={block.key}><p>{blockDescription(block)}</p>
        {block.slot && onSelect && <button type="button" className="f-button" onClick={() => { setSelected([]); onSelect(block.slot!) }}>Open job details</button>}
      </li>)}</ul>
    </DetailDrawer>, document.body)}
  </div>
}
/** Let CSS ellipsize partial names; hide text when even a short fragment cannot fit. */
function LaneLabel({ label }: { label: string }) {
  const ref = useRef<HTMLSpanElement>(null)
  const [fits, setFits] = useState(false)
  useEffect(() => {
    const element = ref.current
    const bar = element?.parentElement
    if (!element || !bar) return
    let active = true
    const measure = () => {
      if (!active) return
      const style = getComputedStyle(bar)
      const labelStyle = getComputedStyle(element)
      const labelPadding = (parseFloat(labelStyle.paddingLeft) || 0) + (parseFloat(labelStyle.paddingRight) || 0)
      const available = bar.clientWidth - (parseFloat(style.paddingLeft) || 0) - (parseFloat(style.paddingRight) || 0) - 2
      setFits(element.scrollWidth <= available || available - labelPadding >= 36)
    }
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    observer?.observe(bar)
    observer?.observe(element)
    void document.fonts?.ready.then(measure)
    measure()
    return () => { active = false; observer?.disconnect() }
  }, [label])
  return <span className="f-lane-label" ref={ref} aria-hidden="true" style={{ visibility: fits ? 'visible' : 'hidden' }}>{label}</span>
}
const ranges = [{ hours: 12, label: '12 hours' }, { hours: 24, label: '24 hours' }, { hours: 72, label: '3 days' }, { hours: 168, label: '7 days' }]
const rangeKey = 'odin-foundry-timeline-hours'
function savedHours() {
  try {
    const hours = Number(localStorage.getItem(rangeKey))
    return ranges.some(range => range.hours === hours) ? hours : 12
  } catch { return 12 }
}
function midnightTicks(start: number, hours: number) {
  const ticks: number[] = []
  const day = new Date(start)
  day.setHours(24, 0, 0, 0)
  while (day.getTime() < start + hours * 3600000) {
    ticks.push(day.getTime())
    day.setDate(day.getDate() + 1)
  }
  return ticks
}
function TimeAxis({ start, hours }: { start: number; hours: number }) {
  const dates = [start, ...midnightTicks(start, hours)]
  const ticks = Array.from({ length: 5 }, (_, i) => start + i * hours / 4 * 3600000)
  return <div className="f-time-axis f-mono"><span /><div className="f-time-labels">
    {hours > 12 && <div className="f-date-line">{dates.map((date, i) => <time key={date} dateTime={new Date(date).toISOString()}
      style={{ '--date-left': `${(date - start) / (hours * 3600000) * 100}%`, top: i % 2 ? 24 : 0 } as CSSProperties}>
      {hours > 24 && `${new Date(date).toLocaleDateString([], { weekday: 'short' })} `}{new Date(date).toLocaleDateString([], { month: 'short', day: 'numeric' })}
    </time>)}</div>}
    {hours <= 24 && <div className="f-hour-line">{ticks.map((tick, i) => <time key={tick} dateTime={new Date(tick).toISOString()} style={{ left: `${i * 25}%` }}>{new Date(tick).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })}</time>)}</div>}
  </div></div>
}
export function Schedule({ printers, prints = [], queue = [], now, rangeControl = false }: { printers: Printer[]; prints?: PrintJob[]; queue?: Job[]; now: number; rangeControl?: boolean }) {
  const chart = useRef<HTMLDivElement>(null)
  const [pocketWidth, setPocketWidth] = useState(148)
  const [selected, setSelected] = useState<TimelineSlot | null>(null)
  const [range, setRange] = useState(savedHours)
  const hours = rangeControl ? range : 12
  const title = `Next ${ranges.find(range => range.hours === hours)!.label}`
  // Preserve the shared Floor query and its cadence. Later dated jobs are already in queue.
  const queryStart = Math.floor(now / 60000) * 60000
  const startDate = new Date(now)
  startDate.setMinutes(0, 0, 0)
  const start = startDate.getTime()
  const query = useQuery({ queryKey: ['timeline', 'foundry', queryStart], queryFn: () => timeline.get(new Date(queryStart).toISOString(), 1), refetchInterval: 30000 })
  const lanes = printers.map(printer => ({ printer, blocks: laneBlocks(printer, query.data?.slots || [], prints, queue, now) }))
  const unknownCount = Math.max(0, ...lanes.map(lane => lane.blocks.filter(block => !hasTime(block)).length))
  const unknownWidth = unknownCount ? `min(${pocketWidth}px, var(--unknown-cap, 148px))` : '0px'
  useLayoutEffect(() => {
    const labels = [...(chart.current?.querySelectorAll<HTMLElement>('.f-lane-pocket-size') || [])]
    if (!labels.length) return
    const measure = () => {
      const width = Math.max(...labels.map(label => label.getBoundingClientRect().width))
      if (width) setPocketWidth(Math.min(148, Math.ceil(width) + 10)) // label, padding and border
    }
    measure()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    labels.forEach(label => observer?.observe(label))
    return () => observer?.disconnect()
  }, [unknownCount, query.isLoading, query.isError])
  return <section className="f-schedule" aria-label={title}><div className="f-row"><h2>{title}</h2><Link to="/jobs">Jobs</Link></div>
    {rangeControl && <label className="f-schedule-range">Show <select aria-label="Schedule range" value={hours} onChange={event => {
      const value = Number(event.target.value)
      setRange(value)
      try { localStorage.setItem(rangeKey, String(value)) } catch { /* Storage can be unavailable. */ }
    }}>{ranges.map(range => <option key={range.hours} value={range.hours}>{range.label}</option>)}</select></label>}
    {query.isError ? <p role="alert">Could not load schedule. <button onClick={() => query.refetch()}>Retry</button></p> : query.isLoading ? <p role="status">Loading schedule…</p> : <>
      <div className="f-schedule-scroll" role="region" aria-label="Printer schedule" tabIndex={0}><div ref={chart} className="f-schedule-chart" style={{ '--unknown-width': unknownWidth, '--schedule-unknown-width': unknownWidth } as CSSProperties}>
        <TimeAxis start={start} hours={hours} />
        <div className="f-schedule-lanes">
          <div className="f-now-track" aria-hidden="true">{hours > 24 && midnightTicks(start, hours).map(tick => <div key={tick} className="f-day-grid-line" data-time={new Date(tick).toISOString()} style={{ left: `${(tick - start) / (hours * 3600000) * 100}%` }} />)}<div className="f-now-line" style={{ left: `${(now - start) / (hours * 3600000) * 100}%` }}><span>Now</span></div></div>
          {lanes.map(({ printer, blocks }) => <ScheduleLane key={printer.id} printer={printer} blocks={blocks} start={start} hours={hours} onSelect={setSelected} />)}
        </div>
      </div></div>{!printers.length && <p>No printers configured.</p>}
    </>}
    <p className="f-legend"><span>Solid: printing · violet dashed: queued · amber dashed: waits for you</span>{unknownCount > 0 && !query.isLoading && !query.isError && <> · <span className="f-time-unset">Time not set</span></>}</p>
    <DetailDrawer open={!!selected} onClose={() => setSelected(null)} title={selected ? displayName(selected) : 'Job details'}>{selected && <BlockDetail block={selected} />}</DetailDrawer>
  </section>
}
export function FoundryTimeline() {
  const { fleet, prints, queue } = useFloorData()
  const now = useNow()
  return <div className="f-page"><div className="f-row"><h1>Timeline</h1><Link className="f-primary" to="/jobs">Schedule a print</Link></div><Schedule printers={fleet.data || []} prints={prints.data} queue={queue.data} now={now} rangeControl /></div>
}
