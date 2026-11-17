import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, type QueryObserverOptions } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Schedule, ScheduleLane, laneBlocks, layoutLane } from './ScheduleLane'
import type { LaneBlock } from './ScheduleLane'
import type { Job, Printer, TimelineSlot } from '../../types'

const api = vi.hoisted(() => ({ timeline: vi.fn() }))
vi.mock('../../api', () => ({ timeline: { get: api.timeline } }))
vi.mock('../jobs/TimelineJobDetail', () => ({ default: ({ block }) => <p>Details for job {block.job_id}</p> }))
const now = Date.parse('2026-11-04T12:00:00Z')
const hour = 3600000
const printer = { id: 1, name: 'Press', gcode_state: 'IDLE', last_seen: new Date().toISOString() } as Printer
const block = (id: number, start: number, duration: number, kind: LaneBlock['kind'] = 'queued'): LaneBlock => ({
  key: String(id), name: `Bracket ${id}`, start: now + start * hour, end: now + (start + duration) * hour, kind,
  slot: { job_id: id } as TimelineSlot,
})
function mount(ui) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return { ...render(<QueryClientProvider client={client}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>), client }
}
beforeEach(() => {
  localStorage.clear()
  api.timeline.mockResolvedValue({ slots: [] })
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(1200)
  vi.stubGlobal('ResizeObserver', class {
    constructor(private callback: ResizeObserverCallback) {}
    observe() { this.callback([{ contentRect: { width: 1200 } } as ResizeObserverEntry], this as unknown as ResizeObserver) }
    disconnect() {}
  })
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.clearAllMocks() })

it('keeps three touching two-hour jobs at their true positions, in keyboard time order', () => {
  const blocks = [block(1, 0, 2), block(2, 2, 2), block(3, 4, 2)]
  const groups = layoutLane([...blocks].reverse(), now, 12, 1200)
  expect(groups.map(g => g.blocks.length)).toEqual([1, 1, 1])
  groups.forEach((g, i) => {
    expect(g.left).toBeCloseTo(i * 100 / 6)
    expect(g.width).toBeCloseTo(100 / 6)
    expect(g.pixels).toBe(200)
    expect(g.row).toBe(0)
  })
  mount(<ScheduleLane printer={printer} blocks={[...blocks].reverse()} start={now} />)
  expect(screen.getAllByRole('button').map(b => b.textContent)).toEqual(['Bracket 1', 'Bracket 2', 'Bracket 3'])
})

it('colors only the held job amber and keeps its queued neighbors violet', () => {
  const slots = [1, 2, 3].map((id, i) => ({ job_id: id, printer_id: 1, display_name: `Part ${id}`, status: 'scheduled', start: new Date(now + i * 2 * hour).toISOString(), end: new Date(now + (i + 1) * 2 * hour).toISOString() })) as TimelineSlot[]
  const blocks = laneBlocks(printer, slots, [], [{ id: 2, hold: true } as Job], now)
  mount(<ScheduleLane printer={printer} blocks={blocks} start={now} />)
  expect([...document.querySelectorAll<HTMLElement>('.f-lane-block')].map(b => b.dataset.kind)).toEqual(['queued', 'waiting', 'queued'])
})

it('keeps tiny jobs at true positions and resolves nearby pointer selections through the lane', () => {
  const blocks = [block(1, 0, 1 / 12), block(2, 1 / 12, 1 / 12, 'waiting'), block(3, 2 / 12, 1 / 12), block(4, 3 / 12, 2)]
  const groups = layoutLane(blocks, now, 12, 1200)
  expect(groups.map(g => g.blocks.length)).toEqual([1, 1, 1, 1])
  groups.slice(0, 3).forEach((bar, i) => {
    expect(bar.left / 100 * 1200).toBeCloseTo(i * 1200 / 144)
    expect(bar.pixels).toBeCloseTo(1200 / 144)
  })
  expect(groups[3].pixels).toBe(200)
  const onSelect = vi.fn()
  const { container } = mount(<ScheduleLane printer={printer} blocks={blocks} start={now} onSelect={onSelect} />)
  const group = screen.getByRole('button', { name: /Bracket 1/ })
  expect(group).toHaveAccessibleName(/^Bracket 1 · queued/)
  expect(group).not.toHaveTextContent('3 jobs')
  expect(container.querySelectorAll('.f-lane-tick')).toHaveLength(0)
  expect([...container.querySelectorAll('.f-lane-block')].slice(0, 3).map(t => t.getAttribute('data-kind'))).toEqual(['queued', 'waiting', 'queued'])
  fireEvent.click(group, { clientX: 0, detail: 1 })
  expect(screen.getByRole('dialog')).toHaveTextContent('Bracket 3')
  fireEvent.click(screen.getAllByRole('button', { name: 'Open job details' })[1])
  expect(onSelect).toHaveBeenCalledWith(blocks[1].slot)
})

it('never merges a 44px job, including between tiny neighbors or at the window edge', () => {
  const groups = layoutLane([block(1, 0, .1), block(2, .1, .44), block(3, .54, .1), block(4, 11.99, .01)], now, 12, 1200)
  expect(groups.map(g => g.blocks.length)).toEqual([1, 1, 1, 1])
  expect(groups.map(g => g.pixels)).toEqual([10, 44, 10, 1])
  expect(groups.every(group => group.row === 0)).toBe(true)
  expect(groups[1].pixels).toBeCloseTo(44)
  // Even the narrow edge job keeps its true start and end.
  expect(groups[3].left / 100 * 1200).toBeCloseTo(1199)
  expect(groups.every(g => g.left + g.width <= 100.00001)).toBe(true)
  const bars = groups.map(group => ({ left: group.left / 100 * 1200, pixels: group.pixels }))
  expect(bars).toHaveLength(4)
  bars.slice(1).forEach((bar, i) => expect(bar.left).toBeGreaterThanOrEqual(bars[i].left + bars[i].pixels - 1e-8))
})

it.each([[200, 'visible'], [80, 'visible'], [44, 'hidden']])('keeps full names and times accessible with %spx for full, ellipsized or hidden text', (width, visibility) => {
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(Number(width))
  vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockReturnValue(190)
  const style = document.createElement('style')
  style.textContent = '.f-lane-block { padding: 4px; }'
  document.head.appendChild(style)
  const job = { ...block(1, 0, 2), name: 'Long bracket assembly' }
  mount(<ScheduleLane printer={printer} blocks={[job]} start={now} />)
  const button = screen.getByRole('button', { name: /Long bracket assembly · queued/ })
  expect(button.getAttribute('aria-label')).toContain(new Date(job.start!).toLocaleString())
  expect(button.getAttribute('aria-label')).toContain(new Date(job.end!).toLocaleString())
  expect(button).toHaveAttribute('title', button.getAttribute('aria-label'))
  expect(button.querySelector<HTMLElement>('.f-lane-label')!.style.visibility).toBe(visibility)
  expect(button.textContent).toBe(job.name)
  style.remove()
})

it('changes every range, header and window without another request, and restores the choice', async () => {
  const queue = [{ id: 9, printer_id: 1, display_name: 'Later part', status: 'scheduled', scheduled_start: new Date(now + 50 * hour).toISOString(), scheduled_end: new Date(now + 52 * hour).toISOString() }] as Job[]
  const props = { printers: [printer], now, queue, rangeControl: true }
  const view = mount(<Schedule {...props} />)
  await screen.findByRole('link', { name: 'Press' })
  for (const [hours, label, dates] of [[12, '12 hours', false], [24, '24 hours', true], [72, '3 days', true], [168, '7 days', true]] as const) {
    fireEvent.change(screen.getByRole('combobox', { name: 'Schedule range' }), { target: { value: hours } })
    expect(screen.getByRole('heading', { name: `Next ${label}` })).toBeInTheDocument()
    expect(Boolean(view.container.querySelector('.f-date-line'))).toBe(dates)
    if (hours <= 24) expect(view.container.querySelector('.f-hour-line time:last-child')).toHaveAttribute('datetime', new Date(now + hours * hour).toISOString())
    else expect(view.container.querySelector('.f-hour-line')).toBeNull()
    expect(Boolean(screen.queryByRole('button', { name: /Later part/ }))).toBe(hours >= 72)
  }
  expect(api.timeline).toHaveBeenCalledExactlyOnceWith(new Date(now).toISOString(), 1)
  expect((view.client.getQueryCache().getAll()[0].options as QueryObserverOptions).refetchInterval).toBe(30000)
  fireEvent.click(screen.getByRole('button', { name: /Later part/ }))
  expect(screen.getByRole('dialog')).toHaveTextContent('Details for job 9')
  expect(localStorage.getItem('odin-foundry-timeline-hours')).toBe('168')
  view.unmount()
  mount(<Schedule {...props} />)
  expect(screen.getByRole('combobox', { name: 'Schedule range' })).toHaveValue('168')
})

it.each(['bad', '0', '13', 'NaN', ''])('falls back to 12 hours for invalid stored range %s', value => {
  localStorage.setItem('odin-foundry-timeline-hours', value)
  mount(<Schedule printers={[]} now={now} rangeControl />)
  expect(screen.getByRole('combobox', { name: 'Schedule range' })).toHaveValue('12')
})

it('still changes range when storage is blocked', () => {
  vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
  mount(<Schedule printers={[]} now={now} rangeControl />)
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 24 } })
  expect(screen.getByRole('heading', { name: 'Next 24 hours' })).toBeInTheDocument()
})

it('keeps an unknown-time pocket separate from a timed bar and leaves empty tracks visible', async () => {
  const unknown = { key: 'unknown', name: 'Undated part', kind: 'waiting' as const }
  const groups = layoutLane([unknown, block(1, 0, 2)], now, 12, 1200)
  expect(groups).toHaveLength(2)
  expect(groups[0]).toMatchObject({ left: 0, unknown: true, row: 0 })
  expect(groups[1].row).toBe(0)
  const view = mount(<ScheduleLane printer={printer} blocks={[unknown, block(1, 0, 2)]} start={now} />)
  expect(screen.getByRole('button', { name: /Undated part · waits for you · Time not set/ })).toHaveTextContent('Time not set')
  view.unmount()
  const empty = mount(<Schedule printers={[printer, { ...printer, id: 2, name: 'Second press' }]} now={now + 30000} />)
  await screen.findByRole('link', { name: 'Second press' })
  expect(empty.container.querySelectorAll('.f-lane-track')).toHaveLength(2)
  expect(empty.container.querySelectorAll('.f-lane-block')).toHaveLength(0)
  expect(parseFloat((empty.container.querySelector('.f-now-line') as HTMLElement).style.left)).toBeCloseTo(30000 / (12 * hour) * 100)
})

it('uses existing maintenance and offline states without labelling unknown as offline', () => {
  const view = mount(<ScheduleLane printer={{ ...printer, maintenance: { state: 'hold' } as Printer['maintenance'], gcode_state: 'OFFLINE' }} blocks={[]} start={now} />)
  expect(screen.getByText('Maintenance hold · Offline')).toBeInTheDocument()
  expect(view.container.querySelector('.f-lane-track')).toBeInTheDocument()
  view.unmount()
  mount(<ScheduleLane printer={{ ...printer, gcode_state: 'UNKNOWN' }} blocks={[]} start={now} />)
  expect(screen.queryByText(/Offline/)).toBeNull()
})

it('dashboard keeps the same separate bars and empty rows at 12 hours without a range control', async () => {
  localStorage.setItem('odin-foundry-timeline-hours', '168')
  api.timeline.mockResolvedValue({ slots: [1, 2, 3].map((id, i) => ({ printer_id: 1, job_id: id, display_name: `Part ${id}`, status: 'scheduled', start: new Date(now + i * 2 * hour).toISOString(), end: new Date(now + (i + 1) * 2 * hour).toISOString() })) })
  const { container } = mount(<Schedule printers={[printer, { ...printer, id: 2, name: 'Empty' }]} now={now} />)
  await waitFor(() => expect(container.querySelectorAll('.f-lane-block')).toHaveLength(3))
  expect(container.querySelectorAll('.f-lane-track')).toHaveLength(2)
  expect(screen.getByRole('heading', { name: 'Next 12 hours' })).toBeInTheDocument()
  expect(screen.queryByRole('combobox')).toBeNull()
  expect(container.querySelector('.f-date-line')).toBeNull()
})

it('shows a complete short name even in a minimum-sized target', () => {
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(44)
  vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockReturnValue(20)
  mount(<ScheduleLane printer={printer} blocks={[{ ...block(1, 0, 1 / 12), name: 'Nut' }]} start={now} />)
  expect(screen.getByText('Nut').style.visibility).toBe('visible')
})

// Review regressions: drawing geometry must be independent of tap geometry.
it.each([24, 72, 168].flatMap(hours => [576, 950].map(width => [hours, width])))('draws all jobs separately at %s hours on a %spx track', (hours, trackWidth) => {
  vi.stubGlobal('ResizeObserver', class {
    constructor(private callback: ResizeObserverCallback) {}
    observe() { this.callback([{ contentRect: { width: trackWidth } } as ResizeObserverEntry], this as unknown as ResizeObserver) }
    disconnect() {}
  })
  const rows = [
    [block(1, 0, 2, 'printing'), block(2, 2, 1, 'waiting'), block(3, 3, 3), block(4, 6, 1.5)],
    [block(5, 0, 1 / 12), block(6, 1 / 12, 1 / 12), block(7, 2 / 12, 1 / 12), block(8, .25, 4)],
  ]
  for (const blocks of rows) {
    const view = mount(<ScheduleLane printer={printer} blocks={blocks} start={now} hours={hours} />)
    const bars = [...view.container.querySelectorAll<HTMLElement>('.f-lane-block')]
    expect(bars).toHaveLength(4)
    bars.forEach((bar, i) => {
      const left = parseFloat(bar.style.left) / 100 * trackWidth
      const width = parseFloat(bar.style.width) / 100 * trackWidth
      const trueLeft = (blocks[i].start! - now) / (hours * hour) * trackWidth
      expect(left).toBeCloseTo(trueLeft)
      expect(width).toBeCloseTo((blocks[i].end! - blocks[i].start!) / (hours * hour) * trackWidth)
      expect(bar.dataset.kind).toBe(blocks[i].kind)
      expect(bar.tagName).toBe('BUTTON')
      expect(bar.style.top).toBe('2px')
      if (i) {
        const previous = bars[i - 1]
        expect(left).toBeGreaterThanOrEqual((parseFloat(previous.style.left) + parseFloat(previous.style.width)) / 100 * trackWidth - 1e-8)
      }
    })
    expect(view.container.querySelector('.f-lane-timed .f-lane-hit')).toBeNull()
    expect(view.container.querySelector('.f-lane-track')).toHaveStyle({ height: '52px' })
    expect(view.container.textContent).not.toMatch(/\d jobs/)
    view.unmount()
  }
})

it('has no mixed-state visual or per-job ticks in a narrow cluster', () => {
  const { container } = mount(<ScheduleLane printer={printer} start={now} hours={168}
    blocks={[block(1, 0, 2, 'printing'), block(2, 2, 1, 'waiting'), block(3, 3, 3)]} />)
  expect(container.querySelector('[data-kind="mixed"]')).toBeNull()
  expect(container.querySelector('.f-lane-tick')).toBeNull()
  for (const id of [1, 2, 3]) expect(screen.getByRole('button', { name: new RegExp(`^Bracket ${id} ·`) })).toBeInTheDocument()
})

it.each([72, 168])('uses weekday/date labels and midnight grid lines for %s hours', async hours => {
  const clock = new Date(2026, 10, 4, 19, 31).getTime()
  localStorage.setItem('odin-foundry-timeline-hours', String(hours))
  const { container } = mount(<Schedule printers={[printer]} now={clock} rangeControl />)
  await screen.findByRole('link', { name: 'Press' })
  expect(container.querySelector('.f-hour-line')).toBeNull()
  const labels = [...container.querySelectorAll('time')]
  expect(labels.length).toBeGreaterThanOrEqual(hours / 24)
  labels.forEach(label => {
    const date = new Date(label.getAttribute('datetime')!)
    expect(label.textContent).toContain(date.toLocaleDateString([], { weekday: 'short' }))
    expect(label.textContent).toContain(String(date.getDate()))
    expect(parseFloat(label.style.getPropertyValue('--date-left'))).toBeCloseTo((date.getTime() - (clock - 31 * 60000)) / (hours * hour) * 100)
  })
  const lines = [...container.querySelectorAll<HTMLElement>('.f-day-grid-line')]
  expect(lines).toHaveLength(hours / 24)
  lines.forEach(line => {
    const date = new Date(line.dataset.time!)
    expect(date.getHours()).toBe(0)
    expect(date.getMinutes()).toBe(0)
    expect(parseFloat(line.style.left)).toBeCloseTo((date.getTime() - (clock - 31 * 60000)) / (hours * hour) * 100)
  })
})

it.each([12, 24, 72, 168])('starts the %s-hour drawing at the hour while preserving the query cadence', async hours => {
  const clock = now + 31 * 60000 + 30000
  localStorage.setItem('odin-foundry-timeline-hours', String(hours))
  const { container } = mount(<Schedule printers={[printer]} now={clock} rangeControl />)
  await screen.findByRole('link', { name: 'Press' })
  expect(parseFloat(container.querySelector<HTMLElement>('.f-now-line')!.style.left)).toBeCloseTo((clock - now) / (hours * hour) * 100)
  expect(api.timeline).toHaveBeenCalledExactlyOnceWith(new Date(now + 31 * 60000).toISOString(), 1)
})

it('keeps unknown work at the left on the same line and aligns the shared time origin after it', async () => {
  const queue = [{ id: 10, printer_id: 1, display_name: 'Undated', status: 'pending' }] as Job[]
  api.timeline.mockResolvedValue({ slots: [{ printer_id: 1, job_id: 1, display_name: 'Timed', status: 'scheduled', start: new Date(now).toISOString(), end: new Date(now + 2 * hour).toISOString() }] })
  const { container } = mount(<Schedule printers={[printer, { ...printer, id: 2 }]} queue={queue} now={now} />)
  await screen.findByRole('button', { name: /Undated/ })
  expect(container.querySelector('.f-schedule-chart')).toHaveStyle({ '--unknown-width': 'min(148px, var(--unknown-cap, 148px))' })
  const pocket = screen.getByRole('button', { name: /Undated/ })
  const timed = screen.getByRole('button', { name: /Timed/ })
  expect(pocket.style.top).toBe(timed.style.top)
  expect(pocket.style.left).toBe('0px')
  expect(container.querySelectorAll('.f-lane-timed')).toHaveLength(2)
})

it.each([390, 1440].flatMap(viewport => [false, true].map(unknown => [viewport, unknown] as const)))('reserves only measured undated content at %spx (unknown: %s)', async (viewport, unknown) => {
  vi.stubGlobal('innerWidth', viewport)
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
    return { width: this.classList.contains('f-lane-pocket-size') ? 88 : viewport } as DOMRect
  })
  const name = 'A printer with a long but fully accessible name'
  const queue = unknown ? [{ id: 10, printer_id: 1, display_name: 'Undated', status: 'pending' } as Job] : []
  const { container } = mount(<Schedule printers={[{ ...printer, name }, { ...printer, id: 2 }]} queue={queue} now={now} />)
  await screen.findByRole('link', { name })
  const chart = container.querySelector<HTMLElement>('.f-schedule-chart')!
  expect(chart.style.getPropertyValue('--unknown-width')).toBe(unknown ? 'min(98px, var(--unknown-cap, 148px))' : '0px')
  expect(screen.getByRole('link', { name })).toHaveAttribute('title', name)
  if (unknown) {
    expect(screen.getByRole('button', { name: /Undated/ }).style.width).toBe('var(--unknown-width)')
    expect(container.querySelector<HTMLElement>('.f-lane')!.style.getPropertyValue('--unknown-width')).toBe('var(--schedule-unknown-width, 148px)')
  }
})

it('identifies the unknown-time pocket in the legend with the same visual key', async () => {
  const queue = [{ id: 10, printer_id: 1, display_name: 'Undated', status: 'pending' }] as Job[]
  const { container } = mount(<Schedule printers={[printer]} queue={queue} now={now} />)
  await screen.findByRole('button', { name: /Undated/ })
  expect(container.querySelector('.f-legend .f-time-unset')).toHaveTextContent('Time not set')
  expect(container.querySelector('.f-lane-pocket')).toHaveClass('f-time-unset')
  expect(container.querySelector('.f-lane-pocket')).toHaveTextContent('Time not set')
  expect(screen.getByRole('button', { name: /Undated/ })).toHaveAccessibleName('Undated · queued · Time not set')
})

it.each(['empty', 'timed', 'loading', 'error'])('omits the time-not-set legend when no pocket is drawn (%s)', async state => {
  const queue = state === 'loading' || state === 'error'
    ? [{ id: 10, printer_id: 1, display_name: 'Undated', status: 'pending' } as Job] : []
  if (state === 'loading') api.timeline.mockReturnValue(new Promise(() => {}))
  if (state === 'error') api.timeline.mockRejectedValue(new Error('Unavailable'))
  if (state === 'timed') api.timeline.mockResolvedValue({ slots: [{ printer_id: 1, job_id: 1, display_name: 'Timed', status: 'scheduled', start: new Date(now).toISOString(), end: new Date(now + hour).toISOString() }] })
  const { container } = mount(<Schedule printers={[printer]} queue={queue} now={now} />)
  if (state === 'error') await screen.findByRole('alert')
  else if (state !== 'loading') await screen.findByRole('link', { name: 'Press' })
  expect(container.querySelector('.f-lane-pocket')).toBeNull()
  expect(container.querySelector('.f-legend .f-time-unset')).toBeNull()
})

it('paints offline status after the now track when there is no pocket column', async () => {
  const { container } = mount(<Schedule printers={[{ ...printer, gcode_state: 'OFFLINE' }]} now={now + 31 * 60000} />)
  const status = await screen.findByText('Offline')
  expect(container.querySelector('.f-schedule-chart')).toHaveStyle({ '--unknown-width': '0px' })
  expect(container.querySelector('.f-now-track')!.compareDocumentPosition(status) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
})
