// F07-7.1-02 — the real Consumables page searches (name or SKU, client side) and filters
// by status (server side, ?status=) over captured producer responses
// (tests/test_contracts/test_cert_C_commerce.py::test_capture_consumables_search_and_status_filter_wire).
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Consumables from './Consumables'
import { consumables } from '../../api'
import fixture from '../../test-fixtures/cert-C-consumables-search-wire.json'

vi.mock('../../api', () => ({ consumables: { list: vi.fn(), adjust: vi.fn(), create: vi.fn(), update: vi.fn(), delete: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: vi.fn(() => true) }))

type Responses = typeof fixture.responses
beforeEach(() => {
  // Serve each request exactly as the backend answered it: no status -> every row.
  vi.mocked(consumables.list).mockImplementation(async (status?: string) =>
    (fixture.responses as Responses)[(status || 'all') as keyof Responses] as any)
})
afterEach(() => { cleanup(); vi.clearAllMocks() })

const ALL = fixture.responses.all.map(row => row.name)

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><Consumables /></QueryClientProvider>)
}

const shown = () => ALL.filter(name => screen.queryByText(name))

it('F07-7.1-02 search matches name or SKU, reports no match, and clearing restores every row', async () => {
  renderPage()
  await screen.findByText('Neodymium magnet 6x3')
  expect(shown()).toEqual(ALL)
  const search = screen.getByLabelText('Search name or SKU')

  fireEvent.change(search, { target: { value: 'magnet' } })          // name, case-insensitive
  expect(shown()).toEqual(['Neodymium magnet 6x3'])

  fireEvent.change(search, { target: { value: 'box-s' } })           // SKU, case-insensitive
  expect(shown()).toEqual(['Shipping box small'])

  fireEvent.change(search, { target: { value: 'no-such-consumable' } })
  expect(shown()).toEqual([])
  expect(screen.getByText('No results match your search')).toBeInTheDocument()

  fireEvent.change(search, { target: { value: '' } })
  expect(shown()).toEqual(ALL)
  expect(vi.mocked(consumables.list).mock.calls).toEqual([[undefined]])  // search never refetches
})

it('F07-7.1-02 status filter asks the server, shows only that status, combines with search and clears', async () => {
  renderPage()
  await screen.findByText('Neodymium magnet 6x3')
  const filter = screen.getByLabelText('Filter by status')

  fireEvent.change(filter, { target: { value: 'discontinued' } })
  await waitFor(() => expect(shown()).toEqual(['Old label roll']))
  expect(consumables.list).toHaveBeenLastCalledWith('discontinued')

  fireEvent.change(filter, { target: { value: 'active' } })
  await waitFor(() => expect(shown()).toEqual(['Heat-set insert M3', 'Neodymium magnet 6x3']))
  fireEvent.change(screen.getByLabelText('Search name or SKU'), { target: { value: 'ins-m3' } })
  expect(shown()).toEqual(['Heat-set insert M3'])
  fireEvent.change(screen.getByLabelText('Search name or SKU'), { target: { value: 'box' } })  // depleted row is not in this response
  expect(shown()).toEqual([])
  expect(screen.getByText('No results match your search')).toBeInTheDocument()

  fireEvent.change(screen.getByLabelText('Search name or SKU'), { target: { value: '' } })
  fireEvent.change(filter, { target: { value: '' } })
  await waitFor(() => expect(shown()).toEqual(ALL))
  expect(consumables.list).toHaveBeenLastCalledWith(undefined)
  const table = screen.getByRole('table')
  expect(within(table).getAllByRole('row').length - 1).toBe(ALL.length)
})
