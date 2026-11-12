import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import SlotTile, { slotLabel, slotState } from './SlotTile'
import PrinterCard from './PrinterCard'
import PrinterRows from './PrinterRows'
import DensityToggle from './DensityToggle'
import NeedsYouBanner, { mayResume } from './NeedsYouBanner'
import CameraView from './CameraView'
import { ScheduleLane, Schedule, laneBlocks, layoutLane } from './ScheduleLane'
import { needsYou } from './data'
import { applyTheme } from '../../hooks/themePreferences'
import type { Printer, PrintJob, Job, Alert, Camera, TimelineSlot } from '../../types'

const mocks = vi.hoisted(() => ({ request: vi.fn(), nozzle: vi.fn(), timeline: vi.fn(), user: { role: 'operator', group_id: 1 } as any, allowed: true, cameraAllowed: true, fallback: false, status: 'live' }))
vi.mock('../../api/client', () => ({ fetchAPI: (...args) => mocks.request(...args) }))
vi.mock('../../api', () => ({ printerTelemetry: { nozzleStatus: (...args) => mocks.nozzle(...args) }, timeline: { get: (...args) => mocks.timeline(...args) } }))
vi.mock('../../permissions', () => ({ canDo: () => mocks.allowed, getCurrentUser: () => mocks.user, canAccessPage: () => mocks.cameraAllowed }))
vi.mock('../../hooks/useWebRTC', () => ({ default: () => ({ videoRef: { current: null }, status: mocks.status, fallback: mocks.fallback, retry: vi.fn() }) }))
vi.mock('../jobs/TimelineJobDetail', () => ({ default: () => <p>Job detail</p> }))

const now = Date.parse('2026-11-04T12:00:00Z')
const printer = (overrides = {}): Printer => ({ id: 1, name: 'Test press', model: 'X1 Carbon', api_type: 'bambu', api_host: '127.0.0.1', has_api_key: true, is_active: true, org_id: 1, last_seen: new Date().toISOString(), gcode_state: 'RUNNING', filament_slots: [], hms_errors: '[]', nozzle_temp: 220, bed_temp: 60, ...overrides } as Printer)
const print = (overrides = {}): PrintJob => ({ id: 8, printer_id: 1, status: 'running', display_name: 'Fixture bracket', progress_percent: 42, current_layer: 12, total_layers: 30, remaining_minutes: 60, started_at: '2026-11-04T11:00:00Z', ...overrides } as unknown as PrintJob)
function mount(ui) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>)
}
beforeEach(() => {
  localStorage.clear(); applyTheme('dark', 'foundry')
  mocks.allowed = true; mocks.cameraAllowed = true; mocks.user = { role: 'operator', group_id: 1 }; mocks.status = 'live'; mocks.fallback = false
  mocks.nozzle.mockResolvedValue({ nozzle_0: { temp: 204 }, nozzle_1: { temp: 198 } })
  mocks.timeline.mockResolvedValue({ slots: [] })
  mocks.request.mockResolvedValue({ success: true })
})
afterEach(() => { cleanup(); applyTheme('dark', 'classic'); vi.clearAllMocks() })

describe('SlotTile', () => {
  it.each([['loaded', 'Loaded'], ['feeding', 'Feeding now'], ['empty', 'No spool'], ['blocked', 'Do not feed'], ['changed', 'Spool changed?'], ['support', 'Support only']] as const)('renders %s with a word and state styling', (state, word) => {
    const { container } = mount(<SlotTile state={state} apiType="bambu" slot={{ slot_number: 1, filament_type: 'PLA', color_hex: 'ff0000' }} />)
    expect(screen.getByText(word)).toBeInTheDocument()
    expect(container.querySelector(`[data-slot-state="${state}"]`)).toBeInTheDocument()
    expect(screen.getByText('Remaining unknown')).toBeInTheDocument()
  })
  it('uses real states and labels, and does not infer feeding or blocked', () => {
    expect(slotState({ slot_number: 1, spool_confirmed: false })).toBe('loaded')
    expect(slotState({ slot_number: 1, filament_type: 'empty' })).toBe('empty')
    expect(slotState({ slot_number: 1, spool_change_pending: true })).toBe('changed')
    expect(slotState({ slot_number: 17, filament_type: 'PLA_SUPPORT' })).toBe('support')
    expect([1, 5, 17, 18, 513].map(n => slotLabel('bambu', n))).toEqual(['AMS 1 · 1', 'AMS 2 · 1', 'HT', 'HT 2', 'Slot 513'])
    expect(slotLabel('anycubic_lan', 1)).toBe('ACE · 1')
  })
  it('keeps long labels in a clamped tooltip and the preset on its own line', () => {
    mount(<SlotTile slot={{ slot_number: 1, color: 'A very long real material name', preset: 'Generic PETG', remaining: 0 }} />)
    expect(screen.getByTitle('A very long real material name')).toHaveClass('f-slot-label')
    expect(screen.getByText('Preset: Generic PETG')).toHaveClass('f-slot-preset')
    expect(screen.getByText('Remaining 0%')).toBeInTheDocument()
  })
  it('does not turn missing ledger data into loaded spools', () => {
    mount(<SlotTile slot={{ slot_number: 1, mapping_status: 'unavailable', remaining: 90, filament_type: 'PLA' }} />)
    expect(screen.getByText('Spool unavailable')).toBeInTheDocument()
    expect(screen.getByText('Remaining unknown')).toBeInTheDocument()
    expect(screen.queryByText('Loaded')).toBeNull()
  })
})
it('card uses reported progress/layers/temperatures, next mapping and HT tiles', async () => {
  mount(<PrinterCard printer={printer({ model: 'H2D', machine_type: 'H2D', filament_slots: [{ id: 1, slot_number: 17, filament_type: 'SUPPORT' }] })} job={print()} now={now} next={{ display_name: 'Fixture plate', ams_mapping: [4], hold: true } as Job} />)
  expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '42')
  expect(screen.getByText('42% · L 12/30')).toBeInTheDocument()
  expect(screen.getByText(/Est. done/)).toBeInTheDocument()
  expect(await screen.findByText('L nozzle 198°')).toBeInTheDocument()
  expect(screen.getByText('R nozzle 204°')).toBeInTheDocument()
  expect(screen.getByText('AMS 2 · 1')).toBeInTheDocument()
  expect(screen.getByText('HT')).toBeInTheDocument()
  expect(screen.queryByText(/plate clear confirmed/i)).toBeNull()
})
it('card omits missing telemetry and does not estimate a paused finish', () => {
  mount(<PrinterCard printer={printer({ gcode_state: 'PAUSED', nozzle_temp: null, bed_temp: null })} job={print({ progress_percent: null, current_layer: null, total_layers: null })} />)
  expect(screen.queryByRole('progressbar')).toBeNull()
  expect(screen.getByText('Progress unknown')).toBeInTheDocument()
  expect(screen.queryByText(/Est. done|Nozzle|Bed/)).toBeNull()
})
it('density persists across mounts and synchronizes other controls', () => {
  const first = mount(<><DensityToggle /><DensityToggle /></>)
  fireEvent.click(screen.getAllByRole('button', { name: 'Compact' })[0])
  expect(localStorage.getItem('odin-foundry-density')).toBe('compact')
  expect(screen.getAllByRole('button', { name: 'Compact' }).every(b => b.getAttribute('aria-pressed') === 'true')).toBe(true)
  first.unmount(); mount(<DensityToggle />)
  expect(screen.getByRole('button', { name: 'Compact' })).toHaveAttribute('aria-pressed', 'true')
  act(() => { localStorage.setItem('odin-foundry-density', 'comfortable'); window.dispatchEvent(new StorageEvent('storage')) })
  expect(screen.getByRole('button', { name: 'Comfortable' })).toHaveAttribute('aria-pressed', 'true')
})
it('compact table provides named columns and a real Resolve link', () => {
  mount(<PrinterRows printers={[printer({ gcode_state: 'PAUSED' })]} prints={[print()]} />)
  expect(screen.getAllByRole('columnheader').map(e => e.textContent)).toEqual(['Printer', 'State', 'Job', 'Progress', 'Temps'])
  expect(screen.getByRole('link', { name: 'Resolve' })).toHaveAttribute('href', '/#needs-you-1')
})
it.each(['PAUSE', 'PAUSED', 'ERROR', 'RUNOUT'])('banner includes %s exactly once and uses API cause/time', status => {
  mount(<NeedsYouBanner printers={[printer({ gcode_state: status })]} prints={[print()]} alerts={[{ id: 1, printer_id: 1, alert_type: 'print_paused', message: 'Server says: filament sensor needs attention.', created_at: '2026-11-04T11:54:00Z' } as Alert]} cameraIds={[1]} now={now} />)
  expect(screen.getAllByRole('article')).toHaveLength(1)
  expect(screen.getByRole('heading', { name: /Server says: filament sensor needs attention/ })).toBeInTheDocument()
  expect(screen.getByText(/Reported 6 minutes ago/)).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Open camera' })).toHaveAttribute('href', '/cameras/1')
})
it('does not mistake serialized empty HMS for an error', () => {
  expect(needsYou(printer())).toBe(false)
  expect(needsYou(printer({ hms_errors: '[{"code":"actual"}]' }))).toBe(true)
})
it('Resume uses existing endpoint and reports acceptance, not confirmation', async () => {
  mount(<NeedsYouBanner printers={[printer({ gcode_state: 'PAUSE' })]} />)
  fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
  await waitFor(() => expect(mocks.request).toHaveBeenCalledWith('/printers/1/resume', { method: 'POST' }))
  expect(await screen.findByRole('status')).toHaveTextContent('Resume requested; waiting for printer confirmation.')
})
it('reports failed resume without saying it resumed', async () => {
  mocks.request.mockRejectedValueOnce(new Error('offline'))
  mount(<NeedsYouBanner printers={[printer({ gcode_state: 'PAUSE' })]} />)
  fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
  expect(await screen.findByRole('status')).toHaveTextContent('Resume was not confirmed')
})
it.each([
  { api_type: 'anycubic_lan' }, { api_host: null }, { has_api_key: false }, { gcode_state: 'RUNNING' }, { org_id: 2 }, { last_seen: null },
])('hides unavailable resume %j', overrides => {
  expect(mayResume(printer({ gcode_state: 'PAUSE', ...overrides }))).toBe(false)
})
it('hides actions for viewers, unavailable cameras and stale causes', () => {
  mocks.user = { role: 'viewer', group_id: 1 }; mocks.allowed = false
  mount(<NeedsYouBanner printers={[printer({ gcode_state: 'PAUSE' })]} prints={[print()]} alerts={[{ printer_id: 1, alert_type: 'print_paused', created_at: '2026-11-03T10:00:00Z', message: 'Old cause' } as Alert]} />)
  expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()
  expect(screen.queryByText('Open camera')).toBeNull()
  expect(screen.queryByText('Old cause')).toBeNull()
  expect(screen.getByText(/No current cause was reported/)).toBeInTheDocument()
})
it('switches cameras, labels paused printers, opens wall and requests fullscreen', async () => {
  const cameras = [{ id: 1, name: 'Press' }, { id: 2, name: 'Paused press' }] as Camera[]
  const { container } = mount(<CameraView cameras={cameras} printers={[printer(), printer({ id: 2, gcode_state: 'PAUSED' })]} prints={[print()]} />)
  expect(screen.getByText('LIVE · Press · WebRTC')).toBeInTheDocument()
  expect(screen.getByText('42% · L 12/30 · Nozzle 220°')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Paused press · paused' }))
  expect(screen.queryByText('LIVE · Press · WebRTC')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Wall' }))
  expect(container.querySelectorAll('video')).toHaveLength(2)
  const fullscreen = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(screen.getByRole('region', { name: 'Cameras' }), 'requestFullscreen', { value: fullscreen })
  fireEvent.click(screen.getByRole('button', { name: 'Full screen' }))
  expect(fullscreen).toHaveBeenCalledOnce()
})
it('keeps the chosen camera button in view by moving the chooser strip only', () => {
  const cameras = [{ id: 1, name: 'Press' }, { id: 2, name: 'Far press' }] as Camera[]
  const isStrip = (el: Element) => el.classList.contains('f-camera-tabs')
  const size = (prop: string, value: number) =>
    vi.spyOn(HTMLElement.prototype, prop as any, 'get').mockImplementation(function (this: HTMLElement) { return isStrip(this) ? value : 0 })
  const spies = [size('scrollWidth', 600), size('clientWidth', 200)]
  // The strip shows x 10..210; "Press" sits at 10..90 and "Far press" at 410..530, both shifted by the scroll.
  const rects = vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    const strip = this.closest('.f-camera-tabs') as HTMLElement | null
    const shift = strip && !isStrip(this) ? strip.scrollLeft : 0
    const [left, width] = isStrip(this) ? [10, 200] : this.textContent === 'Far press' ? [410 - shift, 120] : [10 - shift, 80]
    return { left, right: left + width, top: 0, bottom: 30, width, height: 30, x: left, y: 0, toJSON: () => ({}) } as DOMRect
  })
  const scrollIntoView = vi.fn(); const original = Element.prototype.scrollIntoView
  Element.prototype.scrollIntoView = scrollIntoView
  try {
    mount(<CameraView cameras={cameras} />)
    const strip = screen.getByRole('group', { name: 'Choose camera' })
    expect(strip.scrollLeft).toBe(0)                       // the first camera is already in view
    fireEvent.click(screen.getByRole('button', { name: 'Far press' }))
    expect(strip.scrollLeft).toBe(320)                     // its right edge (530) meets the strip's (210)
    fireEvent.click(screen.getByRole('button', { name: 'Press' }))
    expect(strip.scrollLeft).toBe(0)
    strip.scrollLeft = 150                                 // the user swiped away; the address bar hides (height only)
    fireEvent(window, new Event('resize'))
    expect(strip.scrollLeft).toBe(150)
    const width = window.innerWidth
    try {
      Object.defineProperty(window, 'innerWidth', { value: width + 200, configurable: true })
      fireEvent(window, new Event('resize'))               // a real change of width puts the chosen camera back in view
      expect(strip.scrollLeft).toBe(0)
    } finally { Object.defineProperty(window, 'innerWidth', { value: width, configurable: true }) }
    expect(scrollIntoView).not.toHaveBeenCalled()          // the page itself is never scrolled
  } finally { spies.forEach(spy => spy.mockRestore()); rects.mockRestore(); Element.prototype.scrollIntoView = original }
})
it('camera chip waits for MJPEG frames and never calls snapshots live', async () => {
  mocks.fallback = true; mocks.status = 'error'
  mount(<CameraView cameras={[{ id: 1, name: 'Press' } as Camera]} />)
  expect(screen.getByText('Connecting · Press · MJPEG')).toBeInTheDocument()
  fireEvent.load(screen.getByRole('img', { hidden: true }))
  expect(await screen.findByText('LIVE · Press · MJPEG')).toBeInTheDocument()
  fireEvent.error(screen.getByRole('img'))
  expect(await screen.findByText('Snapshot · Press · Snapshots')).toBeInTheDocument()
  expect(screen.queryByText(/^LIVE/)).toBeNull()
})
it('schedule keeps waits inside the row and opens complete descriptions for short/overlapping jobs', () => {
  const blocks = [
    { key: 'p', name: 'Print', start: now - 3600000, end: now + 3600000, kind: 'printing' as const },
    { key: 'q', name: 'Queue', start: now + 3600000, end: now + 4 * 3600000, kind: 'queued' as const },
    { key: 'w', name: 'Wait', kind: 'waiting' as const },
  ]
  const { container } = mount(<ScheduleLane printer={printer()} blocks={blocks} start={now} />)
  expect(container.querySelectorAll('.f-lane-track')).toHaveLength(1)
  const waits = screen.getByRole('button', { name: /Wait · waits for you · time unknown/ })
  expect(waits.parentElement).toHaveClass('f-lane-track')
  fireEvent.click(waits)
  expect(screen.getByRole('dialog')).toHaveTextContent('Wait · waits for you · time unknown')
  expect(screen.getByRole('link', { name: 'Test press' })).toHaveAttribute('title', 'Test press')
  const wide = layoutLane(blocks.slice(0, 1), now, 12, 1200)
  expect(wide[0].left).toBe(0)
  expect(wide[0].width).toBeCloseTo(100 / 12)
})
it.each([120, 208, 300, 800])('schedule has bounded non-overlapping 44px targets at track width %s', width => {
  const blocks = Array.from({ length: 12 }, (_, i) => ({ key: `${i}`, name: `Job ${i}`, kind: 'queued' as const, start: now + i * 3600000, end: now + i * 3600000 + 60000 }))
  const groups = layoutLane(blocks, now, 12, width)
  expect(groups.flatMap(g => g.blocks)).toHaveLength(12)
  for (let i = 0; i < groups.length; i++) {
    expect(groups[i].pixels).toBeGreaterThanOrEqual(44)
    expect(groups[i].left).toBeGreaterThanOrEqual(0)
    expect(groups[i].left + groups[i].width).toBeLessThanOrEqual(100.00001)
    if (i) expect(groups[i].left).toBeGreaterThanOrEqual(groups[i - 1].left + groups[i - 1].width)
  }
})
it('banner without a job has no leading separator or duplicated paused state', () => {
  mount(<NeedsYouBanner printers={[printer({ name: 'Press · paused', gcode_state: 'PAUSE' })]} alerts={[{ printer_id: 1, alert_type: 'print_paused', message: 'Load filament.', created_at: '2026-11-04T11:53:00Z' } as Alert]} now={now} />)
  expect(screen.getByRole('heading')).toHaveTextContent('Press · paused: Load filament.')
  expect(screen.getByText('Reported 7 minutes ago')).toHaveClass('f-mono')
})
it('H2D compact slots keep AMS/slot addresses and the full long material', () => {
  const slots = [1, 2, 5, 6, 17].map(n => ({ slot_number: n, filament_type: 'PLA_SUPPORT' }))
  const { container } = mount(<PrinterCard printer={printer({ machine_type: 'H2D', filament_slots: slots })} />)
  for (const label of ['1·1', '1·2', '2·1', '2·2', 'HT']) expect(screen.getByText(label)).toBeInTheDocument()
  expect(container.querySelector('.f-slots')).toHaveStyle({ gridTemplateColumns: 'repeat(5, minmax(0, 1fr))' })
  expect(screen.getAllByTitle('PLA_SUPPORT')).toHaveLength(5)
})
it('schedule deduplicates tracked jobs and represents holds and paused waits honestly', () => {
  const slots = [{ printer_id: 1, mqtt_job_id: 8, status: 'printing', start: '2026-11-04T11:00:00Z', end: '2026-11-04T12:00:00Z' }] as TimelineSlot[]
  const blocks = laneBlocks(printer({ gcode_state: 'PAUSE' }), slots, [print()], [{ id: 2, printer_id: 1, status: 'pending', hold: true, item_name: 'Held job' } as Job], now)
  expect(blocks).toHaveLength(2)
  expect(blocks.every(b => b.kind === 'waiting')).toBe(true)
  expect(blocks[0].end).toBeUndefined()
})
it('schedule provides a time axis, per-printer lanes and worded legend', async () => {
  mount(<Schedule printers={[printer()]} now={now} />)
  expect(await screen.findByRole('link', { name: 'Test press' })).toBeInTheDocument()
  expect(screen.getByText('Solid: printing · violet dashed: queued · amber dashed: waits for you')).toBeInTheDocument()
  expect(within(screen.getByRole('region', { name: 'Next 12 hours' })).getAllByText(/\d\d:\d\d/)).toHaveLength(5)
})
it('density remains usable when browser storage is unavailable', () => {
  const storage = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
  try {
    mount(<DensityToggle />)
    fireEvent.click(screen.getByRole('button', { name: 'Compact' }))
    expect(screen.getByRole('button', { name: 'Compact' })).toHaveAttribute('aria-pressed', 'true')
  } finally { storage.mockRestore() }
})

 it('uses compact card slots with accessible feeding state and keeps full detail tiles', () => {
  const props = { printer: printer({ feeding_slot: 1, filament_slots: [{ id: 1, slot_number: 1, filament_type: 'PLA', preset: 'Generic PLA', remaining: 50 }] }) }
  const card = mount(<PrinterCard {...props} />)
  expect(card.container.querySelector('[data-variant="compact"][data-slot-state="feeding"]')).toBeInTheDocument()
  expect(screen.getByText('Feeding now')).toHaveClass('sr-only')
  expect(screen.queryByText(/Remaining|Preset:/)).toBeNull()
  card.unmount()
  mount(<PrinterCard {...props} slotVariant="full" />)
  expect(screen.getByText('Remaining 50%')).toBeInTheDocument()
  expect(screen.getByText('Preset: Generic PLA')).toBeInTheDocument()
})
it.each([null, 'UNKNOWN', 'unknown'])('unknown printer state %s has its own neutral badge', gcode_state => {
  mount(<PrinterCard printer={printer({ gcode_state, last_seen: null })} />)
  expect(screen.getByText('Unknown')).toHaveAttribute('data-status', 'neutral')
  expect(screen.queryByText('Offline')).toBeNull()
})
it('explicit offline is still offline', () => {
  mount(<PrinterCard printer={printer({ gcode_state: 'OFFLINE' })} />)
  expect(screen.getByText('Offline')).toBeInTheDocument()
})
it('shows reported chamber, next nozzle and plate confirmation time', () => {
  mount(<PrinterCard printer={printer({ chamber_temp: 38, plate_clear_confirmed_at: '2026-11-04T18:31:00Z' })}
    next={{ display_name: 'Fixture plate', ams_slot_nozzles: [{ source_index: 0, slot_number: 5, nozzle: 'right' }] } as Job} />)
  expect(screen.getByText('Chamber 38°')).toBeInTheDocument()
  expect(screen.getByText('Right nozzle · AMS 2 · 1')).toBeInTheDocument()
  expect(screen.getByText(/Plate clear confirmed/).querySelector('time')).toHaveAttribute('dateTime', '2026-11-04T18:31:00Z')
})
it('does not invent nozzle, chamber or confirmation when missing', () => {
  mount(<PrinterCard printer={printer({ nozzle_temp: null })}
    next={{ display_name: 'Fixture plate', ams_slot_nozzles: [{ source_index: 0, slot_number: 5, nozzle: null }] } as Job} />)
  expect(screen.getByText('AMS 2 · 1')).toBeInTheDocument()
  expect(screen.queryByText(/nozzle|Chamber|Plate clear/)).toBeNull()
})

it('one slotted job opens its details in one click without an intermediate drawer', () => {
  const onSelect = vi.fn()
  const slot = { job_id: 42 } as TimelineSlot
  mount(<ScheduleLane printer={printer()} blocks={[{ key: 'one', name: 'Single job', kind: 'queued', start: now, end: now + 3600000, slot }]} start={now} onSelect={onSelect} />)
  fireEvent.click(screen.getByRole('button', { name: /Single job/ }))
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(slot)
  expect(screen.queryByRole('dialog')).toBeNull()
})
it('undated waiting work begins at now and retains timed jobs in a grouped drawer', () => {
  const slot = { job_id: 42 } as TimelineSlot
  const blocks = [{ key: 'wait', name: 'Load filament', kind: 'waiting' as const },
    { key: 'scheduled', name: 'Part', kind: 'queued' as const, start: now, end: now + 3600000, slot }]
  const groups = layoutLane(blocks, now, 12, 300)
  expect(groups).toHaveLength(1)
  expect(groups[0].left).toBe(0)
  const onSelect = vi.fn()
  mount(<ScheduleLane printer={printer()} blocks={blocks} start={now} onSelect={onSelect} />)
  fireEvent.click(screen.getByRole('button', { name: /Load filament/ }))
  expect(onSelect).not.toHaveBeenCalled()
  expect(screen.getByRole('dialog')).toHaveTextContent('time unknown')
  fireEvent.click(screen.getByRole('button', { name: 'Open job details' }))
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(slot)
})
it('a single unslotted block still opens its complete facts', () => {
  const onSelect = vi.fn()
  mount(<ScheduleLane printer={printer()} blocks={[{ key: 'wait', name: 'Held part', kind: 'waiting' }]} start={now} onSelect={onSelect} />)
  fireEvent.click(screen.getByRole('button', { name: /Held part/ }))
  expect(screen.getByRole('dialog')).toHaveTextContent('Held part · waits for you · time unknown')
  expect(onSelect).not.toHaveBeenCalled()
})
it('a short block hides a label that cannot fit while preserving its full accessible name', () => {
  const clientWidth = vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(44)
  const scrollWidth = vi.spyOn(HTMLElement.prototype, 'scrollWidth', 'get').mockReturnValue(48)
  try {
    mount(<ScheduleLane printer={printer()} blocks={[{ key: 'short', name: 'Complete job name', kind: 'queued', start: now, end: now + 60000 }]} start={now} />)
    const button = screen.getByRole('button', { name: /Complete job name/ })
    expect(button.querySelector('span')).toHaveStyle({ visibility: 'hidden' })
  } finally { clientWidth.mockRestore(); scrollWidth.mockRestore() }
})
