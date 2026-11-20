import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import EmergencyStop from './EmergencyStop'
const state = vi.hoisted(() => ({ allowed: true, role: 'operator', fetch: vi.fn(), error: vi.fn() }))
vi.mock('react-hot-toast', () => ({ default: { error: state.error } }))
vi.mock('../../api', () => ({ fetchAPI: state.fetch }))
vi.mock('../../permissions', () => ({ canDo: () => state.allowed, getCurrentUser: () => ({ role: state.role }) }))
beforeEach(() => {
 state.allowed = true
 state.role = 'operator'
 state.fetch.mockReset()
 state.error.mockReset()
 state.fetch.mockImplementation(async (path: string) => path === '/printers' ? [{ id: 199, name: 'Synthetic active printer', gcode_state: 'RUNNING', last_seen: new Date().toISOString() }] : { success: true })
})
it.each(['unmount', 'account'])('ignores a delayed command failure after %s', async change => {
 let reject!: (error: Error) => void
 state.fetch.mockImplementation(async (path: string) => path === '/printers' ? [{ id: 199, name: 'Synthetic active printer', gcode_state: 'RUNNING', last_seen: new Date().toISOString() }] : new Promise((_, failed) => { reject = failed }))
 const view = render(<EmergencyStop />)
 fireEvent.click(await screen.findByRole('button', { name: 'Emergency controls, 1 active prints' }))
 fireEvent.click(screen.getByRole('button', { name: 'Stop', exact: true }))
 fireEvent.click(screen.getByRole('button', { name: 'Confirm Stop', exact: true }))
 await waitFor(() => expect(reject).toBeTypeOf('function'))
 if (change === 'unmount') view.unmount()
 else state.role = 'viewer'
 reject(new Error('synthetic delayed failure'))
 await new Promise(resolve => setTimeout(resolve, 0))
 expect(state.error).not.toHaveBeenCalled()
 expect(screen.queryByRole('status')).not.toBeInTheDocument()
})
it('excludes active states with expired heartbeat from command targets', async () => {
 state.fetch.mockResolvedValue([{ id: 199, name: 'Synthetic stale printer', gcode_state: 'RUNNING', last_seen: '2020-01-01T00:00:00Z' }])
 render(<EmergencyStop />)
 fireEvent.click(await screen.findByRole('button', { name: 'Emergency controls, 0 active prints' }))
 expect(screen.getByText('No active prints')).toBeInTheDocument()
 expect(screen.queryByRole('button', { name: 'Stop', exact: true })).not.toBeInTheDocument()
})
it('keeps viewer controls hidden even with a customized action permission', async () => {
 state.role = 'viewer'
 render(<EmergencyStop />)
 await waitFor(() => expect(state.fetch).toHaveBeenCalled())
 expect(screen.queryByRole('button', { name: /Emergency controls/ })).not.toBeInTheDocument()
})
afterEach(() => { cleanup(); vi.useRealTimers() })
it('clears populated targets and pending confirmation after refresh failure', async () => {
 vi.useFakeTimers()
 render(<EmergencyStop />)
 await act(async () => { await Promise.resolve() })
 fireEvent.click(screen.getByRole('button', { name: 'Emergency controls, 1 active prints' }))
 fireEvent.click(screen.getByRole('button', { name: 'Stop', exact: true }))
 state.fetch.mockRejectedValue(new Error('synthetic refresh failure'))
 await act(async () => { vi.advanceTimersByTime(5000); await Promise.resolve() })
 expect(screen.getByRole('alert')).toHaveTextContent('Printer status unavailable')
 expect(screen.queryByRole('button', { name: 'Confirm Stop', exact: true })).not.toBeInTheDocument()
 expect(screen.queryByText('Synthetic active printer')).not.toBeInTheDocument()
 expect(state.fetch.mock.calls.every(call => call[0] === '/printers')).toBe(true)
})
it('ignores an older list response after a newer refresh succeeds', async () => {
 vi.useFakeTimers()
 let resolveOld!: (value: any) => void
 state.fetch.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
 state.fetch.mockResolvedValue([])
 render(<EmergencyStop />)
 await act(async () => { vi.advanceTimersByTime(5000); await Promise.resolve() })
 await act(async () => { resolveOld([{ id: 199, name: 'Stale response target', gcode_state: 'RUNNING', last_seen: new Date().toISOString() }]); await Promise.resolve() })
 fireEvent.click(screen.getByRole('button', { name: 'Emergency controls, 0 active prints' }))
 expect(screen.getByText('No active prints')).toBeInTheDocument()
 expect(screen.queryByText('Stale response target')).not.toBeInTheDocument()
})
it('prevents repeated submissions while a target command is pending', async () => {
 let complete!: (value: any) => void
 state.fetch.mockImplementation(async (path: string) => path === '/printers' ? [{ id: 199, name: 'Synthetic active printer', gcode_state: 'RUNNING', last_seen: new Date().toISOString() }] : new Promise(resolve => { complete = resolve }))
 render(<EmergencyStop />)
 fireEvent.click(await screen.findByRole('button', { name: 'Emergency controls, 1 active prints' }))
 fireEvent.click(screen.getByRole('button', { name: 'Stop', exact: true }))
 const confirm = screen.getByRole('button', { name: 'Confirm Stop', exact: true })
 fireEvent.click(confirm)
 fireEvent.click(confirm)
 expect(state.fetch.mock.calls.filter(call => call[0] === '/printers/199/stop')).toHaveLength(1)
 await act(async () => { complete({ success: true }); await Promise.resolve() })
 expect(screen.getByRole('status')).toHaveTextContent('Command accepted; printer confirmation pending')
})
it('requires explicit confirmation before sending a targeted stop', async () => {
 render(<EmergencyStop />)
 fireEvent.click(await screen.findByRole('button', { name: 'Emergency controls, 1 active prints' }))
 fireEvent.click(screen.getByRole('button', { name: 'Stop', exact: true }))
 expect(state.fetch).toHaveBeenCalledTimes(1)
 fireEvent.click(screen.getByRole('button', { name: 'Confirm Stop', exact: true }))
 await waitFor(() => expect(state.fetch).toHaveBeenCalledWith('/printers/199/stop', { method: 'POST' }))
})
it('does not expose control actions to a principal without printer-control permission', async () => {
 state.allowed = false
 render(<EmergencyStop />)
 await waitFor(() => expect(state.fetch).toHaveBeenCalled())
 expect(screen.queryByRole('button', { name: /Emergency controls/ })).not.toBeInTheDocument()
})
it('reports an unavailable printer list rather than silently hiding controls', async () => {
 state.fetch.mockRejectedValue(new Error('synthetic list failure'))
 render(<EmergencyStop />)
 expect(await screen.findByRole('alert')).toHaveTextContent('Printer status unavailable')
})
it('does not accept an empty command response as confirmed', async () => {
 state.fetch.mockImplementation(async (path: string) => path === '/printers' ? [{ id: 199, name: 'Synthetic active printer', gcode_state: 'RUNNING', last_seen: new Date().toISOString() }] : undefined)
 render(<EmergencyStop />)
 fireEvent.click(await screen.findByRole('button', { name: 'Emergency controls, 1 active prints' }))
 fireEvent.click(screen.getByRole('button', { name: 'Stop', exact: true }))
 fireEvent.click(screen.getByRole('button', { name: 'Confirm Stop', exact: true }))
 expect(await screen.findByRole('status')).toHaveTextContent('Command not confirmed')
})

it('shows the Moonraker receipt and keeps Stop available while Pause awaits confirmation', async () => {
 const printer = { id: 199, name: 'Test printer', api_type: 'moonraker', gcode_state: 'RUNNING', last_seen: new Date().toISOString(), unproven_controls: ['pause', 'stop'] }
 state.fetch.mockImplementation(async (path: string) => path === '/printers' ? [printer] : {
   success: true, message: 'Pause sent. Waiting for the printer to confirm.', outcome: 'pending',
   pending_control: { action: 'pause', sent_at: printer.last_seen, deadline_at: new Date(Date.now() + 30000).toISOString() },
 })
 render(<EmergencyStop />)
 fireEvent.click(await screen.findByRole('button', { name: 'Emergency controls, 1 active prints' }))
 fireEvent.click(screen.getByRole('button', { name: 'Pause (unproven)' }))
 expect(await screen.findByRole('status')).toHaveTextContent('Pause sent. Waiting for the printer to confirm.')
 expect(screen.getByText('Pause sent, waiting for the printer')).toBeInTheDocument()
 expect(screen.getByRole('button', { name: 'Pause (unproven)' })).toBeDisabled()
 fireEvent.click(screen.getByRole('button', { name: 'Stop (unproven)' }))
 expect(screen.getByRole('button', { name: 'Confirm Stop (unproven)' })).toBeEnabled()
 expect(screen.getByText(/use its touchscreen/)).toBeInTheDocument()
})

it.each(['pause', 'resume', 'stop'] as const)('honors the deadline and repeat confirmation for %s', async action => {
 const now = Date.now()
 const printer = { id: 199, name: 'Moonraker', api_type: 'moonraker', gcode_state: action === 'resume' ? 'PAUSED' : 'RUNNING',
   last_seen: new Date(now).toISOString(), pending_control: { action, sent_at: new Date(now - 10000).toISOString(),
     deadline_at: new Date(now + (action === 'stop' ? 30000 : -1)).toISOString() } }
 state.fetch.mockImplementation(async path => path === '/printers' ? [printer] : { success: true })
 render(<EmergencyStop />)
 fireEvent.click(await screen.findByRole('button', { name: /Emergency controls/ }))
 const button = await screen.findByRole('button', { name: new RegExp('^' + action + '$', 'i') })
 expect(button).toBeEnabled()
 if (action === 'stop') {
   fireEvent.click(button)
   fireEvent.click(screen.getByRole('button', { name: /Stop was already sent 10 seconds ago. Send it again\?/ }))
   expect(state.fetch).toHaveBeenCalledWith('/printers/199/stop', { method: 'POST' })
 }
})

it.each([1000, 3000])('allows a confirmed repeat Stop after %i ms', async delay => {
 vi.useFakeTimers()
 const printer = { id: 199, name: 'Test', api_type: 'moonraker', gcode_state: 'RUNNING', last_seen: new Date().toISOString() }
 const replies: Array<(receipt: object) => void> = []
 state.fetch.mockImplementation((path: string) => path === '/printers' ? Promise.resolve([printer])
   : new Promise(resolve => replies.push(resolve)))
 const receipt = { success: true, outcome: 'pending', pending_control: { action: 'stop', sent_at: printer.last_seen,
   deadline_at: new Date(Date.now() + 300000).toISOString() } }
 render(<EmergencyStop />)
 await act(async () => {})
 fireEvent.click(screen.getByRole('button', { name: /Emergency controls/ }))
 fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
 fireEvent.click(screen.getByRole('button', { name: 'Confirm Stop' }))
 await act(async () => { vi.advanceTimersByTime(delay - 1) })
 expect(screen.getByRole('button', { name: 'Sending...' })).toBeDisabled()
 await act(async () => {
   vi.advanceTimersByTime(1)
   if (delay < 3000) replies[0]({ success: true, outcome: 'confirmed' })
 })
 fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
 expect(replies).toHaveLength(1)
 fireEvent.click(screen.getByRole('button', { name: /Send it again\?/ }))
 expect(replies).toHaveLength(2)
 if (delay === 3000) await act(async () => { replies[0](receipt) })
 expect(screen.getByRole('button', { name: 'Sending...' })).toBeDisabled()
 await act(async () => { replies[1](receipt) })
})
