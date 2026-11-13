import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import JobRow from './JobRow'
import { filamentCheck, type FilamentCheck } from '../../api/filamentCheck'

vi.mock('../../api/filamentCheck', () => ({ filamentCheck: { get: vi.fn() } }))
vi.mock('./OutcomeCorrection', () => ({ default: () => null }))

const check: FilamentCheck = {
  job_id: 4, printer_id: 9, mode: 'warn', margin_pct: 10, prints: 1, overall: 'short', would_hold: false, warnings: [],
  slots: [
    { slot: 2, source_slots: [1], material: 'PLA', color: 'black', status: 'short', needed_g: 55, print_g: 50, remaining_g: 40, spool_id: 7, reason: null,
      message: 'Slot 2 (black PLA) has about 40 g left; this print needs about 55 g' },
    { slot: 3, source_slots: [2], material: 'PETG', color: 'white', status: 'unknown', needed_g: null, print_g: null, remaining_g: null, spool_id: null, reason: 'no_spool',
      message: "Slot 3 (white PETG): can't check, no spool is assigned to this slot" },
  ],
}
const job = (over = {}) => ({ id: 4, status: 'scheduled', printer_id: 9, printer: { name: 'P1S-A' }, priority: 3, quantity: 1, hold: false, is_locked: false, ...over })
function mount(j: any) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><table><tbody>
    <JobRow job={j} onAction={vi.fn()} isSelected={false} onToggleSelect={vi.fn()} />
  </tbody></table></QueryClientProvider>)
}

beforeEach(() => { vi.clearAllMocks(); vi.mocked(filamentCheck.get).mockResolvedValue(check) })
afterEach(() => cleanup())

it('does not ask the server until the row is opened, then shows each slot in plain words', async () => {
  mount(job())
  expect(filamentCheck.get).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Filament check' }))
  const panel = await screen.findByRole('status', { name: 'Filament check' })
  expect(filamentCheck.get).toHaveBeenCalledWith(4, 9)
  expect(panel).toHaveTextContent('Slot 2 (black PLA) has about 40 g left; this print needs about 55 g')
  expect(panel).toHaveTextContent("Slot 3 (white PETG): can't check, no spool is assigned to this slot")
  expect(panel).toHaveTextContent('Short:')
  expect(panel).toHaveTextContent('Not checked:')
  fireEvent.click(screen.getByRole('button', { name: 'Hide filament check' }))
  expect(screen.queryByRole('status', { name: 'Filament check' })).toBeNull()
})

it('is read-only: the panel adds no buttons or inputs', async () => {
  mount(job())
  fireEvent.click(screen.getByRole('button', { name: 'Filament check' }))
  const panel = await screen.findByRole('status', { name: 'Filament check' })
  expect(panel.querySelector('button, input, select, a')).toBeNull()
})

it.each([{ status: 'pending' }, { status: 'printing' }, { status: 'scheduled', printer_id: null }])('has no filament check control for %o', over => {
  mount(job(over))
  expect(screen.queryByRole('button', { name: 'Filament check' })).toBeNull()
})
