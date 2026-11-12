// Mobile web pass, slice 2: Archives renders stacked cards under 640 px, the unchanged table above.
import { render, screen, cleanup, fireEvent, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import ArchivesPage from './Archives'

vi.mock('../../api/client', () => ({ fetchAPI: vi.fn() }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
const state = vi.hoisted(() => ({ data: undefined as any }))
vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey }: any) => queryKey[0] === 'archives' ? { data: state.data, isLoading: false, isError: false, refetch: vi.fn() }
    : { data: /ams/i.test(String(queryKey[0])) ? undefined : [] },  // no AMS preview until a printer is chosen
  useMutation: () => ({ mutate: vi.fn(), isPending: false }), useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}))
const item = { id: 9, print_name: 'Synthetic measurement', printer_display: 'Synthetic printer', status: 'completed', actual_duration_seconds: 60, filament_used_grams: 0, tags: ['tag-a'], started_at: null, completed_at: null }
function viewport(phone: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: phone && q === '(max-width: 639px)', addEventListener: vi.fn(), removeEventListener: vi.fn() }))
}
beforeEach(() => { state.data = { items: [item], total: 1 } })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

test('phone: one card per archive, no table, filament zero stays visible', () => {
  viewport(true)
  render(<MemoryRouter><ArchivesPage /></MemoryRouter>)
  const cards = screen.getAllByTestId('row-card')
  expect(cards).toHaveLength(1)
  expect(screen.queryByRole('table')).not.toBeInTheDocument()
  expect(within(cards[0]).getByText('0.0g')).toBeInTheDocument()
  expect(within(cards[0]).getByText('Synthetic printer')).toBeInTheDocument()
  expect(within(cards[0]).getByRole('checkbox', { name: 'Select Synthetic measurement for comparison' })).toBeInTheDocument()
})

test('phone: tapping the name opens the archive detail', () => {
  viewport(true)
  render(<MemoryRouter><ArchivesPage /></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: /Synthetic measurement/ }))
  expect(screen.getByRole('dialog')).toBeInTheDocument()
})

test('640 px and above keeps the table', () => {
  viewport(false)
  render(<MemoryRouter><ArchivesPage /></MemoryRouter>)
  expect(screen.getByRole('table')).toBeInTheDocument()
  expect(screen.queryAllByTestId('row-card')).toHaveLength(0)
})

test('without matchMedia the table renders', () => {
  render(<MemoryRouter><ArchivesPage /></MemoryRouter>)
  expect(screen.getByRole('table')).toBeInTheDocument()
})
