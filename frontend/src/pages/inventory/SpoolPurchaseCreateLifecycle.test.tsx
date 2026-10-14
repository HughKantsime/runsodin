import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Spools from './Spools'
const api = vi.hoisted(() => ({ list: vi.fn(), create: vi.fn(), filaments: vi.fn(), printers: vi.fn() }))
const notice = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }))
vi.mock('react-hot-toast', () => ({ default: notice }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 1 }) }))
vi.mock('../../api', () => ({ spools: api, filaments: { list: api.filaments }, printers: { list: api.printers }, bulkOps: {} }))
vi.mock('../../components/inventory/FilamentLibraryView', () => ({ default: () => null }))
vi.mock('../../components/inventory/SpoolGrid', () => ({ default: () => null }))
beforeEach(() => {
 cleanup(); vi.clearAllMocks(); api.list.mockResolvedValue([]); api.printers.mockResolvedValue([])
 api.filaments.mockResolvedValue([{ id: 'lib_80', brand: 'Owned', name: 'Fixture', material: 'PLA' }])
})
function mount() {
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
 client.setQueryData(['auth', 'me'], { id: 3, group_id: 1, role: 'admin' })
 render(<QueryClientProvider client={client}><MemoryRouter><Spools /></MemoryRouter></QueryClientProvider>); return client
}
async function open() {
 fireEvent.click(screen.getAllByRole('button', { name: 'Add Spool' })[0])
 await screen.findByRole('option', { name: /Owned/ })
 fireEvent.change(within(screen.getByRole('dialog')).getByRole('combobox'), { target: { value: '80' } })
}
function submit() { fireEvent.submit(within(screen.getByRole('dialog')).getByRole('button', { name: 'Add Spool' }).closest('form')!) }
describe('real create mutation lifecycle', () => {
 it.each([['success', true], ['error', true], ['success', false], ['error', false]] as const)('old pending create %s (account switch %s) cannot control replacement modal/cache', async (outcome, switchAccount) => {
  let resolve!: (value: unknown) => void; let reject!: (cause: Error) => void
  api.create.mockReturnValue(new Promise((done, fail) => { resolve = done; reject = fail }))
  const client = mount(); await open(); submit(); await waitFor(() => expect(api.create).toHaveBeenCalledTimes(1))
  expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Add Spool' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Close' }))
  if (switchAccount) await act(async () => client.setQueryData(['auth', 'me'], { id: 4, group_id: 2, role: 'operator' }))
  await open(); fireEvent.click(within(screen.getByRole('dialog')).getByRole('checkbox'))
  fireEvent.change(screen.getByLabelText('Total purchase price'), { target: { value: '55.50' } })
  expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Add Spool' })).not.toBeDisabled()
  const invalidations = vi.spyOn(client, 'invalidateQueries')
  await act(async () => { if (outcome === 'success') resolve({ id: 10 }); else reject(new Error('Old response failed')) })
  expect(screen.getByRole('dialog')).toBeInTheDocument(); expect(screen.getByLabelText('Total purchase price')).toHaveValue('55.50')
  expect(notice.success).not.toHaveBeenCalled(); expect(notice.error).not.toHaveBeenCalled(); expect(invalidations).not.toHaveBeenCalled()
  expect(api.create).toHaveBeenCalledTimes(1)
 })
 it('pending cache-only audience roundtrip resets form and retires old instance without manual rerender', async () => {
  let resolve!: (value: unknown) => void
  api.create.mockReturnValue(new Promise(done => { resolve = done }))
  const client = mount(); await open(); submit(); await waitFor(() => expect(api.create).toHaveBeenCalledTimes(1))
  await act(async () => client.setQueryData(['auth', 'me'], { id: 4, group_id: 2, role: 'operator' }))
  await waitFor(() => expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Add Spool' })).not.toBeDisabled())
  fireEvent.change(within(screen.getByRole('dialog')).getByRole('combobox'), { target: { value: '80' } })
  await act(async () => client.setQueryData(['auth', 'me'], { id: 3, group_id: 1, role: 'admin' }))
  await waitFor(() => expect(within(screen.getByRole('dialog')).getByRole('combobox')).toHaveValue(''))
  fireEvent.click(within(screen.getByRole('dialog')).getByRole('checkbox'))
  fireEvent.change(screen.getByLabelText('Total purchase price'), { target: { value: '88.88' } })
  const invalidations = vi.spyOn(client, 'invalidateQueries')
  await act(async () => resolve({ id: 11 }))
  expect(screen.getByRole('dialog')).toBeInTheDocument(); expect(screen.getByLabelText('Total purchase price')).toHaveValue('88.88')
  expect(notice.success).not.toHaveBeenCalled(); expect(invalidations).not.toHaveBeenCalled()
 })

})
