import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import TrayIdentityConfirm from './TrayIdentityConfirm'
import { printers, spools } from '../../api'
vi.mock('../../api', () => ({ printers: { confirmTrayIdentity: vi.fn() }, spools: { create: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
afterEach(cleanup)
beforeEach(() => vi.clearAllMocks())
const props = { printerId: 1, slot: 4, observedState: 'a'.repeat(32), boundSpool: 56,
  spools: [{ id: 57, filament_brand: 'Elegoo', filament_name: 'PETG' }],
  filaments: [{ id: 'lib_2', display_name: 'Elegoo PETG', source: 'library' }], onConfirmed: vi.fn() }
it('confirms same spool in one tap and hides the resolved prompt', async () => {
  vi.mocked(printers.confirmTrayIdentity).mockResolvedValue({ spool_id: 56, replayed: false })
  render(<TrayIdentityConfirm {...props} />)
  fireEvent.click(screen.getByText('Same spool'))
  await waitFor(() => expect(props.onConfirmed).toHaveBeenCalledOnce())
  expect(printers.confirmTrayIdentity).toHaveBeenCalledWith(1, 4, { observed_state: props.observedState, decision: 'same' })
  expect(screen.queryByText(/may have changed/)).not.toBeInTheDocument()
})
it('picks a new spool and sends the observation token', async () => {
  vi.mocked(printers.confirmTrayIdentity).mockResolvedValue({ spool_id: 57, replayed: false })
  render(<TrayIdentityConfirm {...props} />)
  fireEvent.click(screen.getByText('New spool'))
  fireEvent.change(screen.getByLabelText('New spool'), { target: { value: '57' } })
  fireEvent.click(screen.getByText('Confirm new spool'))
  await waitFor(() => expect(printers.confirmTrayIdentity).toHaveBeenCalledWith(1, 4, { observed_state: props.observedState, decision: 'new', spool_id: 57 }))
})
it('creates the actual operator-selected filament record then confirms its spool', async () => {
  vi.mocked(spools.create).mockResolvedValue({ id: 58 } as any)
  vi.mocked(printers.confirmTrayIdentity).mockResolvedValue({ spool_id: 58, replayed: false })
  render(<TrayIdentityConfirm {...props} />)
  fireEvent.click(screen.getByText('New spool'))
  fireEvent.change(screen.getByLabelText('Filament for new spool'), { target: { value: 'lib_2' } })
  fireEvent.click(screen.getByText('Confirm new spool'))
  await waitFor(() => expect(printers.confirmTrayIdentity).toHaveBeenCalledWith(1, 4, { observed_state: props.observedState, decision: 'new', spool_id: 58 }))
  expect(spools.create).toHaveBeenCalledWith({ filament_id: 2, initial_weight_g: 1000, remaining_weight_g: 1000 })
})
it('keeps a stale-state error visible and does not pretend confirmation succeeded', async () => {
  vi.mocked(printers.confirmTrayIdentity).mockRejectedValue(new Error('Tray observation changed; refresh before confirming'))
  render(<TrayIdentityConfirm {...props} />)
  fireEvent.click(screen.getByText('Same spool'))
  expect(await screen.findByRole('alert')).toHaveTextContent('Tray observation changed')
  expect(props.onConfirmed).not.toHaveBeenCalled()
  expect(screen.getByText('Same spool')).toBeEnabled()
})
