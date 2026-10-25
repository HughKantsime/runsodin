import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import DispatchRecoveryPanel from './DispatchRecoveryPanel'
import { printers } from '../../api'
import { canDo } from '../../permissions'
vi.mock('../../api', () => ({ printers: { dispatchRecovery: vi.fn(), recoverDispatch: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: vi.fn() }))
const evidence = { state: 'uncertain', job_id: 9, job: { status: 'scheduled', held: true }, release_available: true,
  attempt_handle: 'dr1.synthetic', snapshot_handle: 'ds1.synthetic', actions: { hold: true, reconcile_retain: true, release: true }, blockers: [] }
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><DispatchRecoveryPanel printerId={7} /></QueryClientProvider>)
}
beforeEach(() => {
  vi.mocked(canDo).mockReturnValue(true)
  vi.mocked(printers.dispatchRecovery).mockResolvedValue(evidence)
  vi.mocked(printers.recoverDispatch).mockResolvedValue({ outcome: 'released', held: true, claim_retained: false, refresh_required: true })
})
afterEach(() => { cleanup(); vi.resetAllMocks() })
it('requires four explicit checks and explains unseen senders', async () => {
  mount()
  fireEvent.click(await screen.findByRole('button', { name: 'Release fence' }))
  expect(screen.getByRole('dialog')).toBeTruthy()
  expect(screen.getAllByText(/Bambu Studio/).length).toBe(2)
  const confirm = screen.getByRole('button', { name: 'Confirm release' })
  expect(confirm).toBeDisabled()
  const boxes = screen.getAllByRole('checkbox')
  expect(boxes).toHaveLength(4)
  boxes.slice(0,3).forEach(box => fireEvent.click(box))
  expect(confirm).toBeDisabled()
  fireEvent.click(boxes[3]); fireEvent.click(confirm)
  expect(await screen.findByText('Fence released; job remains held. No retry was queued.')).toBeTruthy()
  expect(printers.recoverDispatch).toHaveBeenCalledTimes(1)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh evidence' })).toHaveFocus())
  expect(vi.mocked(printers.recoverDispatch).mock.calls[0][2].attestation).toEqual({ version: 1, printer_checked: true, sender_stopped_and_checked: true, transport_and_device_queues_checked: true, no_pending_dispatch_can_arrive: true })
})
it('read only users have refresh without mutations', async () => {
  vi.mocked(canDo).mockReturnValue(false)
  mount()
  await screen.findByText(/The dispatch result is uncertain/)
  expect(screen.queryByRole('button', { name: 'Hold job' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Refresh evidence' })).toBeTruthy()
})
it('blocks release when telemetry is stale without inviting attestation', async () => {
  vi.mocked(printers.dispatchRecovery).mockResolvedValue({ ...evidence, release_available: false, actions: { ...evidence.actions, release: false }, blockers: ['status_unavailable'] })
  mount()
  expect(await screen.findByRole('button', { name: 'Release fence' })).toBeDisabled()
  expect(screen.queryByRole('checkbox')).toBeNull()
  expect(screen.getByText(/Fresh monitor telemetry is unavailable/)).toBeTruthy()
})
it('unknown outcomes GET only across remount', async () => {
  vi.mocked(printers.recoverDispatch).mockRejectedValue(new Error('outcome_unknown'))
  const view = mount()
  fireEvent.click(await screen.findByRole('button', { name: 'Hold job' }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm hold' }))
  await screen.findByText(/The decision could not be confirmed/)
  await waitFor(() => expect(printers.dispatchRecovery).toHaveBeenCalledTimes(2))
  view.unmount(); mount()
  await waitFor(() => expect(printers.dispatchRecovery).toHaveBeenCalledTimes(3))
  expect(printers.recoverDispatch).toHaveBeenCalledTimes(1)
})
it('prevents duplicate retain submits', async () => {
  vi.mocked(printers.recoverDispatch).mockImplementation(() => new Promise(() => {}))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: 'Record decision and retain fence' }))
  const confirm = screen.getByRole('button', { name: 'Confirm retain' })
  fireEvent.click(confirm); fireEvent.click(confirm)
  await waitFor(() => expect(printers.recoverDispatch).toHaveBeenCalledTimes(1))
  expect(printers.recoverDispatch).toHaveBeenCalledWith(7, 'reconcile-retain', expect.objectContaining({ education_action: 'none' }))
})
