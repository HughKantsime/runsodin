// Cert C (F06) — real Spools page wired to the captured /api/spools producer fixture.
import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Spools from './Spools'
import fixture from '../../test-fixtures/cert-C-spools-wire.json'

const api = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('../../api', () => ({ spools: { list: api.list }, printers: { list: vi.fn().mockResolvedValue([]) }, filaments: { list: vi.fn().mockResolvedValue([]) }, bulkOps: { spools: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 1 }) }))

const rows = fixture.spools as any[]
beforeEach(() => { api.list.mockImplementation(async ({ status }: any) => status ? rows.filter(s => s.status === status) : rows) })
afterEach(() => { cleanup(); vi.clearAllMocks() })
function card(label: string) { return screen.getByText(label, { exact: true }).parentElement!.parentElement! }
async function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter><Spools /></MemoryRouter></QueryClientProvider>)
  await screen.findByText('SPL-CERT0001')
}

it('F06-6.1-02 stat cards count all spools and total active remaining weight from the real response', async () => {
  await mount()
  expect(card('Total Spools')).toHaveTextContent('3')
  expect(card('Active Spools')).toHaveTextContent('2')
  expect(card('Total Filament')).toHaveTextContent('830g')
})

it('F06-6.3-02 stored PA profile from the real response is displayed on the spool card', async () => {
  await mount()
  expect(screen.getByText('K0.042')).toBeInTheDocument()
})

it('F06-6.2-02 selected spools open one batch label sheet for exactly those ids', async () => {
  await mount()
  fireEvent.click(screen.getByRole('checkbox', { name: 'Select all' }))
  fireEvent.click(screen.getByRole('button', { name: 'Print labels' }))
  const link = await screen.findByRole('link', { name: /Open label sheet 1 \(2 labels\)/ })
  expect(link).toHaveAttribute('href', '/api/spools/labels/batch?spool_ids=1,2&size=small')
})

// Fixed (cert-fix-C): Spools.tsx and SpoolCard.tsx formerly derived "low" only from percent_remaining < 20
// and ignored the server is_low_stock flag; the real response flags spool 1 (30 g < 50 g threshold) as low.
it('F06-6.4-03 low-stock card and warning follow the server is_low_stock flag', async () => {
  await mount()
  expect(rows.find(s => s.id === 1).is_low_stock).toBe(true)
  expect(card('Low (<20%)')).toHaveTextContent('1')
  expect(screen.getByText('1 spool running low on filament')).toBeInTheDocument()
})
