// Mobile web pass, slice 2: Print Log renders stacked cards under 640 px, the unchanged table above.
import { render, screen, cleanup, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import PrintLog from './PrintLog'

const state = vi.hoisted(() => ({ data: undefined as any }))
vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: any) => queryKey[0] === 'print-log' ? { data: state.data, isLoading: false, isError: false, refetch: vi.fn() } : { data: [] },
}))
const item = { id: 1, print_name: 'Synthetic print', printer_display: 'Synthetic printer', user_name: 'Sam', status: 'failed', actual_duration_seconds: 3600, filament_used_grams: 12.34, completed_at: null }
function viewport(phone: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: phone && q === '(max-width: 639px)', addEventListener: vi.fn(), removeEventListener: vi.fn() }))
}
beforeEach(() => { state.data = { items: [item], total: 1 } })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

test('phone: one card per entry with printer, user, status and filament; no table', () => {
  viewport(true)
  render(<MemoryRouter><PrintLog /></MemoryRouter>)
  const card = screen.getAllByTestId('row-card')[0]
  expect(screen.queryByRole('table')).not.toBeInTheDocument()
  expect(within(card).getByText('Synthetic print')).toBeInTheDocument()
  expect(within(card).getByText('failed')).toBeInTheDocument()
  expect(within(card).getByText('Synthetic printer')).toBeInTheDocument()
  expect(within(card).getByText('Sam')).toBeInTheDocument()
  expect(within(card).getByText('12.3g')).toBeInTheDocument()
})

test('640 px and above keeps the table', () => {
  viewport(false)
  render(<MemoryRouter><PrintLog /></MemoryRouter>)
  expect(screen.getByRole('table')).toBeInTheDocument()
  expect(screen.queryAllByTestId('row-card')).toHaveLength(0)
})
