import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { jobsWithinReach, layoutLane, ScheduleLane, type LaneBlock } from './ScheduleLane'
import type { Printer, TimelineSlot } from '../../types'

const start = Date.parse('2026-11-04T12:00:00Z')
const hour = 3600000
const job = (id: number, from: number, duration: number, kind: LaneBlock['kind'] = 'queued'): LaneBlock => ({
  key: String(id), name: `Job ${id}`, start: start + from * hour, end: start + (from + duration) * hour,
  kind, slot: { job_id: id } as TimelineSlot,
})
const tiny = () => [job(1, 0, 1 / 12), job(2, 1 / 12, 1 / 12, 'waiting'), job(3, 2 / 12, 1 / 12)]
const printer = { id: 1, name: 'Press', gcode_state: 'IDLE', last_seen: new Date().toISOString() } as Printer
function mount(blocks: LaneBlock[], hours = 12, width = 576) {
  vi.stubGlobal('ResizeObserver', class {
    constructor(private callback: ResizeObserverCallback) {}
    observe() { this.callback([{ contentRect: { width } } as ResizeObserverEntry], this as unknown as ResizeObserver) }
    disconnect() {}
  })
  const onSelect = vi.fn()
  const view = render(<MemoryRouter><ScheduleLane printer={printer} blocks={blocks} start={start} hours={hours} onSelect={onSelect} /></MemoryRouter>)
  const lane = view.container.querySelector<HTMLElement>('.f-lane-timed')!
  // A scrolled track's viewport origin must be subtracted from clientX.
  vi.spyOn(lane, 'getBoundingClientRect').mockReturnValue({ left: -100, width } as DOMRect)
  return { ...view, lane, onSelect }
}
beforeEach(() => { vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(576) })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it.each([12, 24, 72, 168].flatMap(hours => [576, 950].map(width => [hours, width])))('finds every reachable job, nearest first, at %sh on %spx', (hours, width) => {
  const blocks = tiny()
  const groups = layoutLane(blocks, start, hours, width)
  const x = groups[0].left / 100 * width + groups[0].pixels / 2
  expect(jobsWithinReach(groups, x, width).map(b => b.key).sort()).toEqual(['1', '2', '3'])
  if (groups.length === 3) {
    const middle = groups[1].left / 100 * width + groups[1].pixels / 2
    expect(jobsWithinReach(groups, middle, width)[0]).toBe(blocks[1])
  }
  const long = layoutLane([job(4, 0, 3), job(5, 3, 3)], start, hours, width)
  expect(jobsWithinReach(long, 3 / hours * width, width).map(b => b.key)).toEqual(['4', '5'])
  const isolated = layoutLane([job(6, 0, 3)], start, hours, width)
  expect(jobsWithinReach(isolated, 1.5 / hours * width, width).map(b => b.key)).toEqual(['6'])
  expect(jobsWithinReach(isolated, 3 / hours * width + 22, width).map(b => b.key)).toEqual(['6'])
  expect(jobsWithinReach(isolated, 3 / hours * width + 22.01, width)).toEqual([])
})

it('resolves pointer clicks on bars and empty reach space through the lane, with nearest first', () => {
  const blocks = [...tiny(), job(4, 2, 3), job(5, 5, 3)]
  const { lane, onSelect } = mount(blocks)
  fireEvent.click(screen.getByRole('button', { name: /Job 2/ }), { clientX: -94, detail: 1 })
  expect(within(screen.getByRole('dialog')).getAllByRole('listitem').map(li => li.textContent?.split(' · ')[0])).toEqual(['Job 2', 'Job 1', 'Job 3'])
  fireEvent.click(screen.getByRole('button', { name: 'Close' }))
  fireEvent.click(lane, { clientX: -100 + 3.5 / 12 * 576, detail: 1 })
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(blocks[3].slot)
  fireEvent.click(lane, { clientX: -100 + 5 / 12 * 576, detail: 1 })
  expect(within(screen.getByRole('dialog')).getAllByRole('listitem')).toHaveLength(2)
  fireEvent.click(screen.getByRole('button', { name: 'Close' }))
  fireEvent.click(lane, { clientX: -100 + 8 / 12 * 576 + 22, detail: 1 })
  expect(onSelect).toHaveBeenLastCalledWith(blocks[4].slot)
  fireEvent.click(lane, { clientX: -100 + 550, detail: 1 })
  expect(onSelect).toHaveBeenCalledTimes(2)
  expect(screen.queryByRole('dialog')).toBeNull()
})

it('uses a pointer cursor only within reach and resets it when leaving', () => {
  const { lane } = mount([job(1, 2, 3)])
  fireEvent.pointerMove(lane, { clientX: -100 + 2 / 12 * 576 - 22 })
  expect(lane).toHaveStyle({ cursor: 'pointer' })
  fireEvent.pointerMove(lane, { clientX: -100 })
  expect(lane).toHaveStyle({ cursor: 'default' })
  fireEvent.pointerMove(lane, { clientX: -100 + 3 / 12 * 576 })
  expect(lane).toHaveStyle({ cursor: 'pointer' })
  fireEvent.pointerLeave(lane)
  expect(lane).toHaveStyle({ cursor: 'default' })
})

it('opens all four jobs when tapping the middle of a seven-day cluster on a phone track', () => {
  vi.stubGlobal('innerWidth', 390)
  const blocks = [...tiny(), job(4, 3 / 12, 1 / 12)]
  const { lane, onSelect } = mount(blocks, 168, 642)
  fireEvent.click(lane, { clientX: -100 + (2 / 12) / 168 * 642, detail: 1 })
  const items = within(screen.getByRole('dialog')).getAllByRole('listitem')
  expect(items.map(li => li.textContent?.split(' · ')[0]).sort()).toEqual(['Job 1', 'Job 2', 'Job 3', 'Job 4'])
  expect(onSelect).not.toHaveBeenCalled()
})

it.each(['{Enter}', ' '])('opens a narrow job directly by keyboard %s', async key => {
  const blocks = tiny()
  const { onSelect } = mount(blocks)
  const button = screen.getByRole('button', { name: /^Job 2 · waits for you/ })
  button.focus()
  await userEvent.setup().keyboard(key)
  expect(onSelect).toHaveBeenCalledExactlyOnceWith(blocks[1].slot)
  expect(screen.queryByRole('dialog')).toBeNull()
})

