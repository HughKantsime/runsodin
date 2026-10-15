// Cert C (F07) — real Consumables page wired to the captured GET /consumables producer fixture.
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Consumables from './Consumables'
import { consumables } from '../../api'
import fixture from '../../test-fixtures/cert-C-consumables-wire.json'
vi.mock('../../api', () => ({ consumables: { list: vi.fn(), adjust: vi.fn(), create: vi.fn(), update: vi.fn(), delete: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: vi.fn(() => true) }))
beforeEach(() => { vi.mocked(consumables.list).mockResolvedValue(fixture.consumables as any) })
afterEach(() => { cleanup(); vi.clearAllMocks() })
const stat = (label: string) => screen.getAllByText(label, { exact: true }).find(e => e.previousElementSibling?.className.includes('text-2xl'))!.previousElementSibling!.textContent

it('F07 consumable list, server low-stock flag and inventory value render from the real response', async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><Consumables /></QueryClientProvider>)
  await screen.findByText('Cert magnet')
  expect(screen.getByText('Cert box')).toBeInTheDocument()
  expect(stat('Total Items')).toBe('2')
  expect(stat('Low Stock')).toBe('1')
  expect(stat('Active')).toBe('2')
  expect(stat('Inventory Value')).toBe('$20.36')
})
