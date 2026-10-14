import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import Orders from './Orders'
const api = vi.hoisted(() => ({ role: 'admin', list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), updateItem: vi.fn(), addItem: vi.fn(), removeItem: vi.fn(), ship: vi.fn() }))
vi.mock('../../api', () => ({ orders: { list: api.list, get: api.get, create: api.create, update: api.update, updateItem: api.updateItem, addItem: api.addItem, removeItem: api.removeItem, ship: api.ship }, products: { list: vi.fn().mockResolvedValue([{ id: 11, name: 'Fixture gear', sku: 'FG', price: 10 }, { id: 12, name: 'Fixture spacer', sku: 'FS', price: 5 }, { id: 13, name: 'Fixture bracket', sku: 'FB', price: 4 }]) }, orderInvoice: vi.fn() }))
vi.mock('../../permissions', () => ({ canDo: (action: string) => action === 'orders.edit' ? api.role === 'admin' : ['admin','operator'].includes(api.role) }))
const order = { id: 71, order_number: 'ORDER-FIXTURE', customer_name: 'Fixture customer', customer_email: 'fixture@example.test', platform: 'direct', status: 'fulfilled', revenue: 25, platform_fees: 0, payment_fees: 0, shipping_charged: 0, tracking_number: null, item_count: 2, jobs_complete: 2, jobs_total: 2, notes: 'Original note', items: [
  { id: 100, product_id: 11, product_name: 'Fixture gear', product_sku: 'FG', quantity: 2, unit_price: 10, subtotal: 20, fulfilled_quantity: 2, is_fulfilled: true },
  { id: 101, product_id: 12, product_name: 'Fixture spacer', product_sku: 'FS', quantity: 1, unit_price: 5, subtotal: 5, fulfilled_quantity: 1, is_fulfilled: true },
] }
function mount() { return render(<MemoryRouter initialEntries={['/orders']}><Orders /></MemoryRouter>) }
async function ready() { const view = mount(); await screen.findAllByTitle('View Details'); return view }
function dialog() { return screen.getByRole('dialog') }
function field(label: string) { return within(dialog()).getByText(label, { selector: 'label', exact: true }).parentElement!.querySelector('input,select,textarea') as HTMLInputElement }
async function edit() { const view = await ready(); fireEvent.click(screen.getAllByTitle('Edit Order')[0]); await screen.findByRole('button', { name: 'Save Changes' }); return view }
async function shipping() { const view = await ready(); fireEvent.click(screen.getAllByTitle('Mark Shipped')[0]); await screen.findByRole('dialog', { name: 'Mark as Shipped' }); return view }
beforeEach(() => {
  vi.clearAllMocks(); api.role = 'admin'; api.list.mockResolvedValue([order]); api.get.mockResolvedValue(order); api.create.mockResolvedValue(order); api.update.mockResolvedValue(order); api.updateItem.mockResolvedValue({}); api.addItem.mockResolvedValue({}); api.removeItem.mockResolvedValue({}); api.ship.mockResolvedValue({ ...order, status: 'shipped' })
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })
it('F09-9.1-02 actual list/detail show customer, order reference and Product line-item identity', async () => {
  await ready(); expect(screen.getByRole('cell', { name: 'Fixture customer' })).toBeInTheDocument(); expect(screen.getAllByRole('button', { name: 'ORDER-FIXTURE' })).toHaveLength(2)
  fireEvent.click(screen.getAllByTitle('View Details')[0]); await screen.findByRole('dialog', { name: 'Order ORDER-FIXTURE' })
  expect(dialog()).toHaveTextContent('Fixture customer'); expect(dialog()).toHaveTextContent('2x Fixture gear(FG)'); expect(dialog()).toHaveTextContent('Fixture spacer(FS)'); expect(api.get).toHaveBeenCalledWith(71)
})
it('F09-9.1-02 create sends customer/reference and numeric Product quantity/price', async () => {
  api.role = 'operator'; await ready(); fireEvent.click(screen.getByRole('button', { name: 'New Order', exact: true })); fireEvent.change(field('Customer Name'), { target: { value: 'New customer' } }); fireEvent.change(field('Order Number'), { target: { value: 'NEW-REF' } })
  fireEvent.click(screen.getByRole('button', { name: 'Add Item' })); const select = within(dialog()).getByRole('option', { name: /Fixture gear/ }).closest('select')!; fireEvent.change(select, { target: { value: '11' } })
  fireEvent.change(within(dialog()).getByPlaceholderText('Qty'), { target: { value: '3' } }); fireEvent.click(screen.getByRole('button', { name: 'Create Order' }))
  await waitFor(() => expect(api.create).toHaveBeenCalledWith(expect.objectContaining({ customer_name: 'New customer', order_number: 'NEW-REF', items: [{ product_id: 11, quantity: 3, unit_price: 10 }] })))
})
it('F09-9.1-02 realistic customer-name typing preserves input focus and complete text', async () => {
  await ready(); fireEvent.click(screen.getByRole('button', { name: 'New Order', exact: true })); const input = field('Customer Name'); await userEvent.type(input,'New customer')
  expect(input).toHaveFocus(); expect(input).toHaveValue('New customer')
})
it('F09-9.1-03 inline edit loads full data and submits metadata plus update/add/remove Product items', async () => {
  await edit(); expect(field('Customer Name')).toHaveValue('Fixture customer'); expect(field('Order Number')).toHaveValue('ORDER-FIXTURE')
  fireEvent.change(field('Customer Name'), { target: { value: 'Edited customer' } }); fireEvent.change(field('Order Number'), { target: { value: 'EDITED-REF' } }); fireEvent.change(within(dialog()).getAllByPlaceholderText('Qty')[0], { target: { value: '4' } })
  fireEvent.click(within(dialog()).getAllByRole('button', { name: /Remove item/ })[1]); fireEvent.click(screen.getByRole('button', { name: 'Add Item' })); const selects = within(dialog()).getAllByRole('combobox').filter(el => el.querySelector('option[value="13"]')); fireEvent.change(selects.at(-1)!, { target: { value: '13' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save Changes' })); await waitFor(() => expect(api.update).toHaveBeenCalledWith(71,expect.objectContaining({ customer_name: 'Edited customer', order_number: 'EDITED-REF' })))
  await waitFor(() => expect(api.updateItem).toHaveBeenCalledWith(71,100,{ quantity: 4, unit_price: 10 })); expect(api.addItem).toHaveBeenCalledWith(71,{ product_id: 13, quantity: 1, unit_price: 4 }); expect(api.removeItem).toHaveBeenCalledWith(71,101)
})
it('F09-9.1-03 canceling edit sends no metadata or item mutation', async () => {
  await edit(); fireEvent.change(field('Customer Name'), { target: { value: 'Discard me' } }); fireEvent.click(screen.getByRole('button', { name: 'Cancel', exact: true }))
  expect(api.update).not.toHaveBeenCalled(); expect(api.updateItem).not.toHaveBeenCalled(); expect(api.addItem).not.toHaveBeenCalled(); expect(api.removeItem).not.toHaveBeenCalled()
})
it('F09-9.1-03 realistic order-reference edit preserves focus and full value', async () => {
  await edit(); const input = field('Order Number'); await userEvent.clear(input); await userEvent.type(input,'EDITED-REF'); expect(input).toHaveFocus(); expect(input).toHaveValue('EDITED-REF')
})
it.each(['operator','admin'])('F09-9.5-01 %s marks fulfilled order shipped with exact tracking number', async role => {
  api.role = role; await shipping(); fireEvent.change(field('Tracking Number'), { target: { value: 'TRACK-FIXTURE' } }); fireEvent.click(within(dialog()).getByRole('button', { name: 'Mark Shipped', exact: true }))
  await waitFor(() => expect(api.ship).toHaveBeenCalledWith(71,{ tracking_number: 'TRACK-FIXTURE' })); await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
})
it('F09-9.5-01 viewer has no shipping action while retaining readable order details', async () => {
  api.role = 'viewer'; await ready(); expect(screen.queryByTitle('Mark Shipped')).not.toBeInTheDocument(); fireEvent.click(screen.getAllByTitle('View Details')[0]); await screen.findByRole('dialog')
  expect(within(dialog()).queryByRole('button', { name: 'Mark Shipped' })).not.toBeInTheDocument(); expect(api.ship).not.toHaveBeenCalled()
})
it('F09-9.5-01 cancel sends no shipping mutation and failure keeps tracking dialog available', async () => {
  await shipping(); fireEvent.click(within(dialog()).getByRole('button', { name: 'Cancel', exact: true })); expect(api.ship).not.toHaveBeenCalled()
  api.ship.mockRejectedValue(new Error('Fixture failure')); fireEvent.click(screen.getAllByTitle('Mark Shipped')[0]); fireEvent.change(field('Tracking Number'), { target: { value: 'RETAIN-ME' } }); fireEvent.click(within(dialog()).getByRole('button', { name: 'Mark Shipped' }))
  await waitFor(() => expect(api.ship).toHaveBeenCalled()); expect(screen.getByRole('dialog', { name: 'Mark as Shipped' })).toBeInTheDocument(); expect(field('Tracking Number')).toHaveValue('RETAIN-ME')
})
it('F09-9.5-01 realistic tracking-number typing retains input focus and all characters', async () => {
  await shipping(); const input = field('Tracking Number'); await userEvent.type(input,'TRACK-FIXTURE'); expect(input).toHaveFocus(); expect(input).toHaveValue('TRACK-FIXTURE')
})
it('F09-9.5-01 pending shipping cannot submit the same shipment twice', async () => {
  const finish: ((value: any) => void)[] = []; api.ship.mockImplementation(() => new Promise(resolve => finish.push(resolve))); await shipping(); const button = within(dialog()).getByRole('button', { name: 'Mark Shipped' })
  fireEvent.click(button); fireEvent.click(button)
  try { expect(api.ship).toHaveBeenCalledTimes(1) } finally { await act(async () => { finish.forEach(resolve => resolve(order)) }) }
})

it('F09-9.5-01 a committed shipment remains closed when refreshing its details fails', async () => {
  await ready(); fireEvent.click(screen.getAllByTitle('View Details')[0]); await screen.findByRole('dialog', { name: 'Order ORDER-FIXTURE' })
  fireEvent.click(within(dialog()).getByRole('button', { name: 'Mark Shipped', exact: true })); await screen.findByRole('dialog', { name: 'Mark as Shipped' })
  api.get.mockRejectedValueOnce(new Error('Read failure after shipment'))
  fireEvent.click(within(screen.getByRole('dialog', { name: 'Mark as Shipped' })).getByRole('button', { name: 'Mark Shipped', exact: true }))
  await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
  expect(screen.queryByRole('dialog', { name: 'Mark as Shipped' })).not.toBeInTheDocument()
  expect(api.ship).toHaveBeenCalledTimes(1)
  expect(screen.queryByTitle('Mark Shipped')).not.toBeInTheDocument()
  expect(within(screen.getByRole('dialog', { name: 'Order ORDER-FIXTURE' })).queryByRole('button', { name: 'Mark Shipped', exact: true })).not.toBeInTheDocument()
})

it('F09-9.5-01 failed list refresh preserves committed shipping status and tracking', async () => {
  await shipping(); fireEvent.change(field('Tracking Number'), { target: { value: 'COMMITTED-TRACK' } })
  api.ship.mockResolvedValueOnce({ ...order, status: 'shipped', tracking_number: 'COMMITTED-TRACK' })
  api.list.mockRejectedValueOnce(new Error('Owned list refresh failure'))
  fireEvent.click(within(dialog()).getByRole('button', { name: 'Mark Shipped', exact: true }))
  await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Mark as Shipped' })).not.toBeInTheDocument())
  expect(screen.queryByTitle('Mark Shipped')).not.toBeInTheDocument()
  expect(api.ship).toHaveBeenCalledTimes(1)
})
