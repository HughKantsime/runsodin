// Cert C (F08) — real Products page wired to the captured GET /products producer fixture.
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Products from './Products'
import { products, models, consumables } from '../../api'
import fixture from '../../test-fixtures/cert-C-products-wire.json'
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({role:'admin'}), canDo: () => true }))
vi.mock('../../api', () => ({ products: { list: vi.fn(), get: vi.fn(), update: vi.fn(), removeComponent: vi.fn(), addComponent: vi.fn() }, models: { list: vi.fn() }, consumables: { list: vi.fn() } }))
beforeEach(() => {
  vi.mocked(products.list).mockResolvedValue(fixture.products as any)
  vi.mocked(models.list).mockResolvedValue([])
  vi.mocked(consumables.list).mockResolvedValue([])
})
afterEach(() => { cleanup(); vi.clearAllMocks() })
const value = (label: string) => screen.getAllByText(label, { exact: true }).find(e => e.tagName === 'DIV')!.parentElement!.firstElementChild!.textContent

it('F08-8.1-02 stat cards summarise the real catalog response', async () => {
  render(<Products />); await screen.findAllByText('Own product')
  expect(value('Products')).toBe('2')
  expect(value('Components')).toBe('1')
  expect(value('With SKU')).toBe('1')
  expect(value('Avg Price')).toBe('$16.00')
})

it('F08-8.1-01/8.2-03 name, SKU, price and BOM COGS estimate from the real response are shown', async () => {
  render(<Products />); await screen.findAllByText('Own product')
  expect(screen.getAllByText('OWN-1').length).toBeGreaterThan(0)
  expect(screen.getByText('Est. COGS: $8.00')).toBeInTheDocument()
  expect(screen.getByText('Est. COGS: Unknown')).toBeInTheDocument()
})
