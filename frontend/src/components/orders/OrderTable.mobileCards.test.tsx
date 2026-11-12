// Mobile web pass, slice 3: the phone order card carries the same fields and actions as a table row.
import { cleanup, render } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import OrderTable from './OrderTable'

vi.mock('../../permissions', () => ({ canDo: () => true }))

const order = { id: 7, order_number: 'ETSY-100', platform: 'etsy', customer_name: 'Ada', status: 'pending', item_count: 3, revenue: 12.5, created_at: '2026-10-13T12:00:00' }
const noop = () => {}
function show() {
  return render(<OrderTable orders={[order]} STATUS_CLASSES={{}} onDetail={noop} onEdit={noop} onSchedule={noop} onShip={noop} onCancel={noop} onDelete={noop} />)
}
const titles = (el: Element) => Array.from(el.querySelectorAll('button[title]')).map(b => b.getAttribute('title'))
afterEach(cleanup)

test('card shows the same fields as the table row', () => {
  const { container } = show()
  const card = container.querySelector('.md\\:hidden') as HTMLElement
  const row = container.querySelector('tbody tr') as HTMLElement
  const date = new Date(order.created_at).toLocaleDateString()
  for (const text of ['ETSY-100', 'Ada', 'etsy', '$12.50', date]) {
    expect(card.textContent).toContain(text)
    expect(row.textContent).toContain(text)
  }
  expect(card.textContent).toMatch(/pending/i)
  expect(row.textContent).toMatch(/pending/i)
  expect(card.textContent).toContain('Items: 3')
})

test('card offers the same row actions as the table row', () => {
  const { container } = show()
  const card = container.querySelector('.md\\:hidden') as HTMLElement
  const row = container.querySelector('tbody tr') as HTMLElement
  expect(titles(card).length).toBeGreaterThan(1)
  expect(titles(card)).toEqual(titles(row))
})

test('card actions get 44 px targets under 640 only; the 640+ switch is unchanged', () => {
  const { container } = show()
  const card = container.querySelector('.md\\:hidden') as HTMLElement
  expect(card.className).toBe('block md:hidden divide-y divide-[var(--brand-card-border)]')
  expect(card.querySelector('button[title="Edit Order"]')!.parentElement!.className).toContain('max-sm:[&_button]:min-h-11')
  expect(container.querySelector('table')!.className).toContain('hidden md:table')
})
