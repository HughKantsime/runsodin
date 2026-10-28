import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import RfidOnboarding from './RfidOnboarding'
import { canDo, getCurrentUser } from '../../permissions'
import { spools, type RfidRoll } from '../../api/inventory'
vi.mock('../../api/inventory', () => ({ spools: { rfidOnboarding: vi.fn(), mergeRfidRoll: vi.fn() } }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 1 }) }))
vi.mock('../../permissions', () => ({ canDo: vi.fn(), getCurrentUser: vi.fn() }))
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(canDo).mockReturnValue(true)
  vi.mocked(getCurrentUser).mockReturnValue({ username: 'operator', role: 'operator' })
})
afterEach(cleanup)
const bound: RfidRoll = { spool_id: 56, tray_uuid: 'a'.repeat(32), color_name: 'Nardo Gray', filament_name: 'PLA Matte', outcome: 'bound', printer_id: 1, slot: 1, found_at: '2026-10-27T12:00:00', possible_duplicate: false, checked: true }
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><RfidOnboarding /></QueryClientProvider>)
  return client
}
it('shows each recorded roll with bound/created outcome and duplicate warning', async () => {
  vi.mocked(spools.rfidOnboarding).mockResolvedValue([bound, { ...bound, spool_id: 66, outcome: 'created', possible_duplicate: true }])
  mount()
  expect(await screen.findByText('2 rolls found')).toBeInTheDocument()
  expect(screen.getByText(/Bound to existing inventory/)).toBeInTheDocument()
  expect(screen.getByText(/Created in inventory/)).toBeInTheDocument()
  expect(screen.getByText(/Possible duplicate:/)).toBeInTheDocument()
  expect(screen.getAllByRole('listitem')).toHaveLength(2)
  expect(spools.rfidOnboarding).toHaveBeenCalledWith(1)
})
it('shows loading and empty instructions', async () => {
  vi.mocked(spools.rfidOnboarding).mockResolvedValue([])
  mount()
  expect(screen.getByRole('status')).toHaveTextContent('Checking for rolls')
  expect(await screen.findByText(/No RFID rolls found yet/)).toBeInTheDocument()
})
it('retries a failed refresh and preserves the returned checklist', async () => {
  vi.mocked(spools.rfidOnboarding).mockRejectedValueOnce(new Error('offline')).mockResolvedValue([bound])
  mount()
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not refresh')
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
  await waitFor(() => expect(screen.getByText('1 roll found')).toBeInTheDocument())
  expect(screen.getAllByRole('listitem')).toHaveLength(1)
})

it('merges a possible duplicate in one tap and refreshes the checklist', async () => {
  vi.mocked(spools.rfidOnboarding).mockResolvedValue([{ ...bound, spool_id: 66, outcome: 'created', possible_duplicate: true, possible_duplicate_spool_ids: [56, 57] }])
  vi.mocked(spools.mergeRfidRoll).mockImplementation(async () => {
    vi.mocked(spools.rfidOnboarding).mockResolvedValue([{ ...bound, outcome: 'merged' }])
    return { spool_id: 56, archived_spool_id: 66 }
  })
  mount()
  fireEvent.click(await screen.findByRole('button', { name: 'Use sealed #56' }))
  await waitFor(() => expect(spools.mergeRfidRoll).toHaveBeenCalledWith(66, 56))
  expect(await screen.findByText(/Merged into sealed inventory/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Use sealed #56' })).not.toBeInTheDocument()
})
it('shows a merge refusal reason without dropping the roll', async () => {
  vi.mocked(spools.rfidOnboarding).mockResolvedValue([{ ...bound, possible_duplicate: true, possible_duplicate_spool_ids: [57] }])
  vi.mocked(spools.mergeRfidRoll).mockRejectedValue(new Error('Cannot merge: source has job usage or deductions'))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: 'Use sealed #57' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('job usage or deductions')
  expect(screen.getAllByRole('listitem')).toHaveLength(1)
})
it('hides merge actions without edit permission', async () => {
  vi.mocked(canDo).mockReturnValue(false)
  vi.mocked(spools.rfidOnboarding).mockResolvedValue([{ ...bound, possible_duplicate: true, possible_duplicate_spool_ids: [57] }])
  mount()
  await screen.findByText('1 roll found')
  expect(screen.queryByRole('button', { name: 'Use sealed #57' })).not.toBeInTheDocument()
})
