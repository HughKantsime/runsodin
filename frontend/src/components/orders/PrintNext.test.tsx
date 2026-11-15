import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import PrintNext, { age } from './PrintNext'
import PrintNextCard from './PrintNextCard'
import { replenishment, PrintNextList, PrintNextRow } from '../../api/replenishment'

const access = vi.hoisted(() => ({ can: true, licensed: true }))
vi.mock('../../permissions', () => ({ canDo: (action: string) => access.can && action === 'products.assortment_preview' }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ hasFeature: (feature: string) => access.licensed && feature === 'products' }) }))
vi.mock('../../api/replenishment', () => ({ replenishment: { list: vi.fn(), queue: vi.fn() } }))

const base: PrintNextRow = { product_id: 1, product_name: 'Mini', reorder_point: 5, target: 14, minimum_batch: 1, status: 'below', reason: 'Needs 12 more to reach 14.', below_point: true,
  on_hand: { quantity: 2, read_at: '2026-11-13T08:00:00Z', age_seconds: 20, source: 'square' }, shortfall: 12, plates_needed: 3,
  recipe: { id: 'a'.repeat(32), name: 'Mini plate', pieces_per_plate: 5, printer_id: 3, materials: 'PLA black', hours_per_plate: 2, hours_per_piece: 0.4 },
  rank: 1, rank_reason: 'Open orders need 30 and stock is 2.', on_the_way: { pieces: 0, job_ids: [], awaiting_stock: 0 },
  open_orders: { pieces: 30, oldest: '2026-10-01 00:00:00' }, velocity: { units_per_week: 3, source: 'ODIN orders', weeks: 4 } }
const rows: PrintNextRow[] = [base,
  { ...base, product_id: 2, product_name: 'Dragon', rank: 2, rank_reason: 'Out of stock.', reason: 'Needs 10 more to reach 10.', plates_needed: 1, shortfall: 10, target: 10, open_orders: null, velocity: null,
    on_hand: { quantity: 0, read_at: '2026-11-10T08:00:00Z', age_seconds: 3 * 86400, source: 'snapshot' }, recipe: { ...base.recipe!, id: 'b'.repeat(32), name: 'Dragon plate', hours_per_plate: null, hours_per_piece: null } },
  { ...base, product_id: 3, product_name: 'Covered part', rank: null, rank_reason: null, plates_needed: 0, shortfall: 0, recipe: null, open_orders: null, velocity: null,
    reason: 'Below its point, but 10 piece(s) already on the way cover it.', on_the_way: { pieces: 10, job_ids: [7, 8], awaiting_stock: 4 } },
  { ...base, product_id: 4, product_name: 'Unmapped part', status: 'blocked', rank: null, rank_reason: null, on_hand: null, shortfall: null, plates_needed: 0, recipe: null, below_point: null, open_orders: null, velocity: null,
    reason: 'Map this product to a Square item to see its stock.' },
  { ...base, product_id: 5, product_name: 'Full shelf', status: 'ok', rank: null, rank_reason: null, shortfall: 0, plates_needed: 0, recipe: null, below_point: false, open_orders: null, velocity: null, reason: 'At or above its reorder point.',
    on_hand: { quantity: 40, read_at: '2026-11-13T08:00:00Z', age_seconds: 20, source: 'square' } }]
const list = (over: Partial<PrintNextList> = {}): PrintNextList => ({ generated_at: '2026-11-13T08:00:20Z', rows, truncated: false, print_next_count: 2,
  stock: { connected: true, read_at: '2026-11-13T08:00:00Z', age_seconds: 20, source: 'square', error: null, refresh_in_seconds: 100, cache_seconds: 120 }, ...over })
const item = (name: string) => screen.getByRole('heading', { name: new RegExp(name) }).closest('li')!

beforeEach(() => { vi.resetAllMocks(); access.can = true; access.licensed = true; vi.mocked(replenishment.list).mockResolvedValue(list()) })
afterEach(() => { delete document.documentElement.dataset.themeFamily; cleanup(); vi.useRealTimers() })

describe.each([['Classic', undefined], ['Foundry', 'foundry']])('Print next list (%s)', (_name, family) => {
  beforeEach(() => { if (family) document.documentElement.dataset.themeFamily = family })

  it('lists products most urgent first with the reason on each row', async () => {
    render(<PrintNext />)
    await screen.findByRole('heading', { name: /Mini/ })
    expect(replenishment.list).toHaveBeenCalledWith()
    const order = within(screen.getByRole('list', { name: 'Products, most urgent first' })).getAllByRole('heading', { level: 3 }).map(h => h.textContent)
    expect(order).toEqual(['1. Mini Print', '2. Dragon Print', 'Covered part Covered', 'Unmapped part Blocked', 'Full shelf In stock'])
    expect(within(item('Mini')).getByText(/Open orders need 30 and stock is 2\. Needs 12 more to reach 14\./)).toBeVisible()
    expect(within(item('Mini')).getByText('2 · read just now')).toBeVisible()
    expect(within(item('Mini')).getByText('5 / 14')).toBeVisible()
    expect(within(item('Mini')).getByText('3 a week (ODIN orders, last 4 weeks)')).toBeVisible()
    expect(within(item('Mini')).getByText(/Mini plate · 5 per plate · about 2 h per plate · PLA black/)).toBeVisible()
    expect(within(item('Dragon')).getByText(/Out of stock\./)).toBeVisible()
    expect(within(item('Dragon')).getByText('0 · last recorded 3 days ago')).toBeVisible()
    expect(within(item('Dragon')).getByText(/print time unknown/)).toBeVisible()
    expect(within(item('Covered part')).getByText('10 (4 inspected, not yet in Square)')).toBeVisible()
    expect(within(item('Unmapped part')).getByText('Map this product to a Square item to see its stock.')).toBeVisible()
    expect(within(item('Unmapped part')).getAllByText('Unknown')).toHaveLength(2)
  })

  it('offers a queue button only where plates are suggested and queues them on hold', async () => {
    vi.mocked(replenishment.queue).mockResolvedValue({ queued: [{ product_id: 1, recipe_id: 'a'.repeat(32), plates: 3, job_ids: [11, 12, 13], held: true }], skipped: [] })
    render(<PrintNext />)
    await screen.findByRole('heading', { name: /Mini/ })
    expect(screen.getAllByRole('button', { name: /^Queue \d+ plate/ })).toHaveLength(2)
    for (const name of ['Covered part', 'Unmapped part', 'Full shelf']) expect(within(item(name)).queryByRole('button')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Queue 1 plate for Dragon' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Queue 3 plates for Mini' }))
    await screen.findByText('Queued 3 plates for Mini, on hold. Release them from Jobs when ready.')
    expect(replenishment.queue).toHaveBeenCalledWith([{ product_id: 1, recipe_id: 'a'.repeat(32), plates: 3 }], false)
    expect(replenishment.list).toHaveBeenCalledTimes(2)  // the list is read again, so the queued plates show as on the way
  })

  it('queues everything suggested in one request, as ready when the operator asks, and reports what was skipped', async () => {
    vi.mocked(replenishment.queue).mockResolvedValue({ queued: [{ product_id: 1, recipe_id: 'a'.repeat(32), plates: 3, job_ids: [11, 12, 13], held: false }],
      skipped: [{ product_id: 2, reason: 'Nothing more is needed: stock and work already on the way cover it.' }] })
    render(<PrintNext />)
    await screen.findByRole('heading', { name: /Mini/ })
    fireEvent.click(screen.getByLabelText('Queue as ready (skip the hold)'))
    fireEvent.click(screen.getByRole('button', { name: 'Queue all suggested (4 plates)' }))
    await screen.findByText('Queued 3 plates for Mini, ready to schedule.')
    expect(replenishment.queue).toHaveBeenCalledWith([{ product_id: 1, recipe_id: 'a'.repeat(32), plates: 3 }, { product_id: 2, recipe_id: 'b'.repeat(32), plates: 1 }], true)
    expect(screen.getByText('Not queued for Dragon: Nothing more is needed: stock and work already on the way cover it.')).toBeVisible()
  })

  it('stacks for a phone: no table, full-width 44px buttons, wrapping text', async () => {
    const { container } = render(<PrintNext />)
    await screen.findByRole('heading', { name: /Mini/ })
    expect(container.querySelector('table')).toBeNull()
    for (const control of screen.getAllByRole('button')) { expect(control).toHaveClass('w-full', 'sm:w-auto', 'min-h-[44px]') }
    for (const entry of screen.getAllByRole('listitem')) expect(entry).toHaveClass('break-words')
    expect(item('Mini').querySelector('dl')).toHaveClass('grid-cols-2', 'sm:grid-cols-4')
    expect(container.innerHTML).not.toMatch(/min-w-\[|w-\[\d+px\]|whitespace-nowrap/)
  })

  it('limits a large suggestion to 50 plates and shows the amount it will queue', async () => {
    vi.mocked(replenishment.list).mockResolvedValue(list({ rows: [{ ...base, plates_needed: 51 }] }))
    vi.mocked(replenishment.queue).mockResolvedValue({ queued: [], skipped: [] })
    render(<PrintNext />)
    fireEvent.click(await screen.findByRole('button', { name: 'Queue 50 plates for Mini' }))
    await waitFor(() => expect(replenishment.list).toHaveBeenCalledTimes(2))
    expect(replenishment.queue).toHaveBeenCalledExactlyOnceWith([{ product_id: 1, recipe_id: base.recipe!.id, plates: 50 }], false)
    expect(screen.getByText(/51 plates suggested; queue up to 50 at a time/)).toBeVisible()
  })

  it('limits bulk queueing to the first 100 products and 50 plates per product', async () => {
    const many = Array.from({ length: 101 }, (_, index) => ({ ...base, product_id: index + 1, product_name: `Part ${index + 1}`, plates_needed: 51 }))
    vi.mocked(replenishment.list).mockResolvedValue(list({ rows: many }))
    vi.mocked(replenishment.queue).mockResolvedValue({ queued: [], skipped: [] })
    render(<PrintNext />)
    fireEvent.click(await screen.findByRole('button', { name: 'Queue next 100 products (5000 plates)' }))
    await waitFor(() => expect(replenishment.list).toHaveBeenCalledTimes(2))
    expect(replenishment.queue).toHaveBeenCalledExactlyOnceWith(many.slice(0, 100).map(r => ({ product_id: r.product_id, recipe_id: r.recipe!.id, plates: 50 })), false)
    expect(screen.getByText(/Queue up to 100 products at a time/)).toBeVisible()
  })
})

it('keeps Refresh off until Square may be read again', async () => {
  vi.useFakeTimers()
  render(<PrintNext />)
  await act(async () => { await Promise.resolve() })
  expect(screen.getByRole('button', { name: 'Refresh' })).toBeDisabled()
  expect(screen.getByTestId('print-next-stock')).toHaveTextContent('Counts are read at most every 2 minutes; the next read is possible in about 100 seconds.')
  await act(async () => { vi.advanceTimersByTime(100_000) })
  expect(screen.getByRole('button', { name: 'Refresh' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
  await act(async () => { await Promise.resolve() })
  expect(replenishment.list).toHaveBeenCalledTimes(2)
})

it('says when Square could not be read and that recorded counts are shown', async () => {
  vi.mocked(replenishment.list).mockResolvedValue(list({ stock: { connected: true, read_at: null, age_seconds: null, source: 'snapshot', error: 'Square is turned off while ITAR mode is on: this server does not contact public services.', refresh_in_seconds: 0, cache_seconds: 120 } }))
  render(<PrintNext />)
  const note = await screen.findByText(/Square could not be read: Square is turned off while ITAR mode is on/)
  expect(note).toHaveAttribute('role', 'status')
  expect(note).toHaveTextContent('Showing the last recorded counts with their age. Nothing is guessed.')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(within(item('Dragon')).getByText('0 · last recorded 3 days ago')).toBeVisible()
})

it('a failed queue request is reported and nothing is claimed', async () => {
  vi.mocked(replenishment.queue).mockRejectedValue(new Error('Square connection changed. Reload the list before queueing.'))
  render(<PrintNext />)
  await screen.findByRole('heading', { name: /Mini/ })
  fireEvent.click(screen.getByRole('button', { name: 'Queue 3 plates for Mini' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Square connection changed')
  expect(screen.queryByText(/^Queued/)).not.toBeInTheDocument()
})

it('renders nothing and reads nothing without the permission', () => {
  access.can = false
  const { container } = render(<PrintNext />)
  expect(container).toBeEmptyDOMElement()
  expect(replenishment.list).not.toHaveBeenCalled()
})

it('explains an empty list', async () => {
  vi.mocked(replenishment.list).mockResolvedValue(list({ rows: [], print_next_count: 0 }))
  render(<PrintNext />)
  expect(await screen.findByText(/No active products yet/)).toBeVisible()
  expect(screen.queryByRole('button', { name: /Queue/ })).not.toBeInTheDocument()
})

it.each([[20, 'just now'], [600, '10 minutes ago'], [7200, '2 hours ago'], [3 * 86400, '3 days ago']])('describes a count %i seconds old as %s', (seconds, text) => {
  expect(age(seconds)).toBe(text)
})

describe.each([['Classic', undefined], ['Foundry', 'foundry']])('Dashboard card (%s)', (_name, family) => {
  beforeEach(() => { if (family) document.documentElement.dataset.themeFamily = family })
  const card = () => render(<MemoryRouter><PrintNextCard /></MemoryRouter>)

  it('appears when a product is below its point, from recorded counts only', async () => {
    card()
    expect(await screen.findByRole('heading', { name: 'Print next: 2 products below their reorder point' })).toBeVisible()
    expect(replenishment.list).toHaveBeenCalledWith(true, true)  // cached: the dashboard never triggers a Square read
    expect(screen.getByText('Mini: 2 on hand, 3 plates suggested')).toBeVisible()
    expect(screen.getByText('Dragon: 0 on hand, 1 plate suggested')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Open Print next' })).toHaveAttribute('href', '/products')
    expect(screen.queryByRole('button')).not.toBeInTheDocument()  // the card queues nothing
  })

  it('stays away when nothing is below its point, the read fails, or the install or role has no access', async () => {
    vi.mocked(replenishment.list).mockResolvedValue(list({ print_next_count: 0 }))
    const first = card()
    await waitFor(() => expect(replenishment.list).toHaveBeenCalledTimes(1))
    expect(first.container).toBeEmptyDOMElement()
    cleanup()
    vi.mocked(replenishment.list).mockRejectedValue(new Error('offline'))
    const second = card()
    await waitFor(() => expect(replenishment.list).toHaveBeenCalledTimes(2))
    expect(second.container).toBeEmptyDOMElement()
    cleanup()
    access.licensed = false
    expect(card().container).toBeEmptyDOMElement()
    access.licensed = true; access.can = false
    expect(card().container).toBeEmptyDOMElement()
    expect(replenishment.list).toHaveBeenCalledTimes(2)
  })
})
