import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, expect, it, vi } from 'vitest'
import FilamentCheckPanel from './FilamentCheckPanel'
import { filamentCheck, type FilamentCheck } from '../../api/filamentCheck'

vi.mock('../../api/filamentCheck', () => ({ filamentCheck: { get: vi.fn() } }))

const short: FilamentCheck = {
  job_id: 4, printer_id: 9, mode: 'warn', margin_pct: 10, prints: 1, overall: 'short', would_hold: false, warnings: [],
  slots: [{ slot: 2, source_slots: [1], material: 'PLA', color: 'black', status: 'short', needed_g: 55, print_g: 50, remaining_g: 40, spool_id: 7, reason: null,
    message: 'Slot 2 (black PLA) has about 40 g left; this print needs about 55 g' }],
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><FilamentCheckPanel jobId={4} printerId={9} /></QueryClientProvider>)
}

beforeEach(() => { vi.clearAllMocks(); vi.mocked(filamentCheck.get).mockResolvedValue(short) })

it('asks for the job on the chosen printer and shows the short slot in plain words', async () => {
  mount()
  const panel = await screen.findByRole('status', { name: 'Filament check' })
  expect(filamentCheck.get).toHaveBeenCalledWith(4, 9)
  expect(panel).toHaveTextContent('dispatch will go ahead with a warning')
  expect(panel).toHaveTextContent('Slot 2 (black PLA) has about 40 g left; this print needs about 55 g')
  expect(panel).toHaveTextContent('Short:')
})

it('says dispatch will be refused when the mode is hold', async () => {
  vi.mocked(filamentCheck.get).mockResolvedValue({ ...short, mode: 'hold', would_hold: true })
  mount()
  expect(await screen.findByRole('status', { name: 'Filament check' })).toHaveTextContent('dispatch will be refused')
})

it('reports unchecked slots without calling them short', async () => {
  vi.mocked(filamentCheck.get).mockResolvedValue({ ...short, overall: 'unknown', would_hold: false,
    slots: [{ ...short.slots[0], status: 'unknown', reason: 'no_spool', message: "Slot 2 (black PLA): can't check, no spool is assigned to this slot" }] })
  mount()
  const panel = await screen.findByRole('status', { name: 'Filament check' })
  expect(panel).toHaveTextContent('could not be fully checked')
  expect(panel).toHaveTextContent('Not checked:')
  expect(panel).not.toHaveTextContent('Short:')
})

it('shows a plain confirmation when every slot has enough', async () => {
  vi.mocked(filamentCheck.get).mockResolvedValue({ ...short, overall: 'enough', slots: [{ ...short.slots[0], status: 'enough', remaining_g: 400 }] })
  mount()
  expect(await screen.findByRole('status', { name: 'Filament check' })).toHaveTextContent('Filament looks sufficient')
})

it('renders nothing when the check is off', async () => {
  vi.mocked(filamentCheck.get).mockResolvedValue({ ...short, mode: 'off', overall: 'off', slots: [] })
  const { container } = mount()
  await vi.waitFor(() => expect(filamentCheck.get).toHaveBeenCalled())
  await vi.waitFor(() => expect(container).toBeEmptyDOMElement())
})

it('degrades to an unavailable notice when the read fails', async () => {
  vi.mocked(filamentCheck.get).mockRejectedValue(new Error('down'))
  mount()
  expect(await screen.findByText(/Filament check unavailable/)).toBeInTheDocument()  // after the loading line
})
