// cert-D orders: real Orders page + real api client + real permissions module, driven by
// HTTP responses captured from the backend routers (certDorders.wire.json, produced by
// tests/test_contracts/test_cert_D_orders_wire_fixture.py). Only window.fetch is stubbed.
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import toast from 'react-hot-toast'
import Orders from './Orders'
import { refreshPermissions } from '../../permissions'
import wire from './certDorders.wire.json'

type Call = { method: string; url: string; body: any }
let calls: Call[] = []
let stage: 'initial' | 'scheduled' | 'shipped' = 'initial'
let role = 'admin'
const ids = wire.ids as Record<string, number>
const pdfBytes = Uint8Array.from(atob(wire.invoice_magic_b64), c => c.charCodeAt(0))

function json(body: any, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function route(method: string, url: string): Response {
  const path = url.split('?')[0]
  if (path === '/api/auth/me') return json({ id: 3, username: 'wire-user', role, group_id: 1 })
  if (path === '/api/permissions') return json(wire.permissions)
  if (path === '/api/products') return json(wire.products)
  if (path === '/api/orders' && method === 'GET') return json(stage === 'initial' ? wire.list_initial : stage === 'scheduled' ? wire.list_after_schedule : wire.list_after_ship)
  const detail = path.match(/^\/api\/orders\/(\d+)$/)
  if (detail && method === 'GET') {
    const id = Number(detail[1])
    if (id === ids.pending && stage !== 'initial') return json(wire.detail_pending_after_schedule)
    if (id === ids.fulfilled && stage === 'shipped') return json(wire.detail_fulfilled_after_ship)
    const key = Object.keys(ids).find(k => ids[k] === id)!
    return json((wire.detail_initial as any)[key])
  }
  if (detail && method === 'PATCH') return json(wire.edit_wholesale)
  if (/^\/api\/orders\/\d+\/items\/\d+$/.test(path) && method === 'PATCH') return json({})
  if (path === `/api/orders/${ids.pending}/schedule` && method === 'POST') { stage = 'scheduled'; return json(wire.schedule_pending) }
  if (path === `/api/orders/${ids.fulfilled}/ship` && method === 'PATCH') { stage = 'shipped'; return json(wire.ship_fulfilled) }
  if (path === `/api/orders/${ids.fulfilled}/invoice.pdf`) return new Response(pdfBytes, { status: 200, headers: wire.invoice_headers })
  return json({ detail: `unrouted ${method} ${path}` }, 599)
}

beforeEach(async () => {
  calls = []; stage = 'initial'; role = 'admin'
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit = {}) => {
    const method = (init.method || 'GET').toUpperCase()
    calls.push({ method, url, body: init.body ? JSON.parse(String(init.body)) : undefined })
    return route(method, url)
  }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

async function mount(as = 'admin') {
  role = as
  expect(await refreshPermissions()).not.toBeNull()
  const view = render(<MemoryRouter initialEntries={['/orders']}><Orders /></MemoryRouter>)
  await screen.findAllByTitle('View Details')
  return view
}
function card(label: string) { return screen.getByText(label, { selector: 'span' }).closest('div')!.parentElement! }
function value(label: string) { return card(label).querySelector('.font-mono')!.textContent }
async function openDetail(index: number, name: string) {
  fireEvent.click(screen.getAllByTitle('View Details')[index])
  return await screen.findByRole('dialog', { name: `Order ${name}` })
}
const rowIndex = (orderNumber: string) => (stage === 'initial' ? wire.list_initial : stage === 'scheduled' ? wire.list_after_schedule : wire.list_after_ship).findIndex((o: any) => o.order_number === orderNumber)

describe('certD orders wire contract', () => {
  it('F09-9.1-04 stat cards show total orders, pending count, revenue and revenue-weighted average margin from backend rows', async () => {
    await mount()
    expect(value('Total Orders')).toBe('3')
    expect(value('Pending')).toBe('1')
    // (40 + 31.5) / (60 + 40) over the two non-cancelled orders with known estimates
    expect(value('Average Margin')).toBe('71.5%')
    expect(card('Average Margin')).toHaveTextContent('2 of 2 orders')
    expect(value('Revenue')).toBe('$200.00')   // includes the cancelled order (see defect test below)
  })

  // DEFECT: frontend/src/pages/orders/Orders.tsx:31-32 Revenue card sums revenue of every order including
  // cancelled ones (WIRE-WHL-3, $100 cancelled) while Average Margin excludes them; expected $100.00.
  it.fails('F09-9.1-04 revenue card excludes cancelled orders', async () => {
    await mount()
    expect(value('Revenue')).toBe('$100.00')
  })

  it('F09-9.1-01/02 detail shows platform, customer, reference and Product line items from backend detail', async () => {
    await mount()
    const dialog = await openDetail(rowIndex('WIRE-ETSY-1'), 'WIRE-ETSY-1')
    expect(dialog).toHaveTextContent('Platform: etsy')
    expect(dialog).toHaveTextContent('Customer: Wire Customer')
    expect(dialog).toHaveTextContent('2x Own product')
    expect(dialog).toHaveTextContent('$60.00')               // line subtotal 2 x 30
    expect(calls.some(c => c.method === 'GET' && c.url === `/api/orders/${ids.fulfilled}`)).toBe(true)
  })

  it('F09-9.3 detail P&L shows revenue, estimated BOM COGS, estimated profit/margin per order and unknown actuals', async () => {
    await mount()
    const dialog = await openDetail(rowIndex('WIRE-ETSY-1'), 'WIRE-ETSY-1')
    const row = (label: string) => within(dialog).getByText(label).parentElement!
    expect(row('Revenue')).toHaveTextContent('$60.00')
    expect(row('Estimated COGS (BOM)')).toHaveTextContent('$17.00')
    expect(row('Est. Profit')).toHaveTextContent('$40.00')
    expect(row('Est. Margin')).toHaveTextContent('66.7%')
    for (const label of ['Actual Cost', 'Profit', 'Margin']) expect(row(label)).toHaveTextContent('Unknown')
  })

  it('F09-9.4-03 Download Invoice button in detail modal fetches the PDF endpoint and saves the PDF blob', async () => {
    const created: Blob[] = []
    const createObjectURL = vi.fn((blob: Blob) => { created.push(blob); return 'blob:certD' })
    Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    await mount('operator')
    const dialog = await openDetail(rowIndex('WIRE-ETSY-1'), 'WIRE-ETSY-1')
    const button = within(dialog).getByTitle('Download Invoice PDF')
    expect(button).toHaveTextContent('Invoice')
    fireEvent.click(button)
    await waitFor(() => expect(click).toHaveBeenCalled())
    expect(calls.filter(c => c.url.endsWith('/invoice.pdf')).map(c => c.url)).toEqual([`/api/orders/${ids.fulfilled}/invoice.pdf`])
    const bytes = new Uint8Array(await created[0].arrayBuffer())
    expect(new TextDecoder().decode(bytes)).toBe('%PDF-')
    expect((click.mock.instances[0] as unknown as HTMLAnchorElement).download).toBe('invoice_WIRE-ETSY-1.pdf')
  })

  it('F09-9.2-01/02 Schedule Jobs on a pending order posts the schedule route and shows created job count, then In Progress', async () => {
    const success = vi.spyOn(toast, 'success')
    await mount('admin')
    fireEvent.click(screen.getAllByTitle('Schedule Jobs')[0])
    await waitFor(() => expect(success).toHaveBeenCalledWith('Created 1 jobs for this order'))
    expect(calls.filter(c => c.method === 'POST').map(c => c.url)).toEqual([`/api/orders/${ids.pending}/schedule`])
    await waitFor(() => expect(value('In Progress')).toBe('1'))
    expect(value('Pending')).toBe('0')
    expect(screen.queryAllByTitle('Schedule Jobs')).toHaveLength(0)
  })

  it('F09-9.5-01 operator marks a fulfilled order shipped with a tracking number; list then shows Shipped', async () => {
    await mount('operator')
    expect(screen.getAllByTitle('Mark Shipped').length).toBeGreaterThan(0)
    fireEvent.click(screen.getAllByTitle('Mark Shipped')[0])
    const dialog = await screen.findByRole('dialog', { name: 'Mark as Shipped' })
    fireEvent.change(within(dialog).getByPlaceholderText('Enter tracking number (optional)'), { target: { value: '1ZWIRE0001' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /Mark Shipped/ }))
    await waitFor(() => expect(value('Shipped')).toBe('1'))
    expect(calls.filter(c => c.method === 'PATCH')).toEqual([{ method: 'PATCH', url: `/api/orders/${ids.fulfilled}/ship`, body: { tracking_number: '1ZWIRE0001' } }])
    expect(value('Fulfilled')).toBe('0')
    const detail = await openDetail(rowIndex('WIRE-ETSY-1'), 'WIRE-ETSY-1')
    expect(detail).toHaveTextContent('Tracking: 1ZWIRE0001')
  })

  it('F09-9.5-01 viewer role gets no Mark Shipped action (backend permission document)', async () => {
    await mount('viewer')
    expect(screen.queryAllByTitle('Mark Shipped')).toHaveLength(0)
    expect(screen.queryAllByTitle('Edit Order')).toHaveLength(0)
  })

  it('F09-9.1-03 admin edit modal loads backend detail and PATCHes the edited fields', async () => {
    await mount('admin')
    fireEvent.click(screen.getAllByTitle('Edit Order')[rowIndex('WIRE-WHL-3')])
    const dialog = await screen.findByRole('dialog', { name: 'Edit Order WIRE-WHL-3' })
    const field = (label: string) => within(dialog).getByText(label, { selector: 'label', exact: true }).parentElement!.querySelector('input,select,textarea') as HTMLInputElement
    expect(field('Customer Name')).toHaveValue('Shop Partner')
    expect(field('Platform')).toHaveValue('wholesale')
    fireEvent.change(field('Customer Name'), { target: { value: 'Edited Partner' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save Changes' }))
    await waitFor(() => expect(calls.some(c => c.method === 'PATCH' && c.url === `/api/orders/${ids.wholesale}`)).toBe(true))
    const patch = calls.find(c => c.method === 'PATCH' && c.url === `/api/orders/${ids.wholesale}`)!
    expect(patch.body).toMatchObject({ customer_name: 'Edited Partner', platform: 'wholesale', order_number: 'WIRE-WHL-3', revenue: 100 })
    // Unchanged line item is re-sent with its backend quantity/price.
    expect(calls.find(c => c.method === 'PATCH' && /\/items\//.test(c.url))?.body).toEqual({ quantity: 4, unit_price: 25 })
  })
})
