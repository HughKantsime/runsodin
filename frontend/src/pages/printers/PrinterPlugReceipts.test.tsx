import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Printers from './Printers'
const state = vi.hoisted(() => ({ user: { id: 3, role: 'operator', group_id: 1 } as any, allowed: true, orgId: 1, on: vi.fn(), off: vi.fn(), read: vi.fn() }))
vi.mock('../../permissions', () => ({ canDo: () => state.allowed, getCurrentUser: () => state.user }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, atPrinterLimit: () => false }) }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: state.orgId }) }))
vi.mock('../../hooks/useCameraList', () => ({ default: () => ({ data: [] }) }))
vi.mock('../../api', async original => ({ ...await original<any>(), plugPowerOn: state.on, plugPowerOff: state.off, getPlugState: state.read }))
afterEach(cleanup)
beforeEach(() => { state.user = { id: 3, role: 'operator', group_id: 1 }; state.allowed = true; state.orgId = 1; state.on.mockReset(); state.off.mockReset(); state.read.mockReset().mockResolvedValue({ power_state: false }) })
function page(extra = {}) {
 const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
 client.setQueryData(['printers', 1], [{ id: 199, org_id: 1, name: 'Synthetic plug', is_active: true, api_type: 'bambu', filament_slots: [], tags: [], plug_type: 'mqtt', last_seen: new Date().toISOString(), ...extra }])
 const tree = () => <QueryClientProvider client={client}><MemoryRouter><Printers /></MemoryRouter></QueryClientProvider>
 const view = render(tree())
 return { ...view, client, refresh: () => view.rerender(tree()) }
}
it('uses a returned measured false instead of inverting the switch', async () => {
 state.on.mockResolvedValue({ power_state: false })
 page()
 fireEvent.click(await screen.findByRole('button', { name: 'Power on plug' }))
 await waitFor(() => expect(state.on).toHaveBeenCalledOnce())
 await waitFor(() => expect(screen.getByRole('button', { name: 'Power on plug' })).toBeInTheDocument())
})
it('leaves physical state unknown after broker acceptance and shows pending confirmation', async () => {
 state.on.mockResolvedValue({ accepted: true, power_state: null })
 page()
 fireEvent.click(await screen.findByRole('button', { name: 'Power on plug' }))
 expect(await screen.findByRole('status')).toHaveTextContent('Command accepted; physical confirmation pending')
 expect(screen.getByText('State unknown')).toBeInTheDocument()
 expect(screen.queryByRole('button', { name: 'Power off plug' })).not.toBeInTheDocument()
})
it('reads the actual API power_state field', async () => {
 state.read.mockResolvedValue({ power_state: true })
 page()
 expect(await screen.findByRole('button', { name: 'Power off plug' })).toBeInTheDocument()
})
it.each([undefined, {}, { accepted: false, power_state: true }, { accepted: true, power_state: 'on' }])('does not announce acceptance for malformed receipt %j', async receipt => {
 state.on.mockResolvedValue(receipt)
 page()
 fireEvent.click(await screen.findByRole('button', { name: 'Power on plug' }))
 expect(await screen.findByRole('status')).toHaveTextContent('Power command not confirmed')
 expect(screen.queryByRole('button', { name: 'Power off plug' })).not.toBeInTheDocument()
})
it.each(['viewer', 'permission', 'foreign-shared', 'unknown-owner'])('denies plug mutations for %s', async reason => {
 if (reason === 'viewer') state.user.role = 'viewer'
 if (reason === 'permission') state.allowed = false
 page(reason === 'foreign-shared' ? { org_id: 2, shared: true } : reason === 'unknown-owner' ? { org_id: undefined } : {})
 await waitFor(() => expect(state.read).toHaveBeenCalled())
 expect(screen.queryByRole('button', { name: 'Power on plug' })).not.toBeInTheDocument()
 expect(state.on).not.toHaveBeenCalled()
})
it('rechecks live permission at click', async () => {
 page()
 const button = await screen.findByRole('button', { name: 'Power on plug' })
 state.allowed = false
 fireEvent.click(button)
 expect(state.on).not.toHaveBeenCalled()
})
it('guards duplicate commands while pending', async () => {
 let done: any
 state.on.mockImplementation(() => new Promise(resolve => { done = resolve }))
 page()
 const button = await screen.findByRole('button', { name: 'Power on plug' })
 fireEvent.click(button); fireEvent.click(button)
 expect(state.on).toHaveBeenCalledOnce(); expect(button).toBeDisabled()
 await act(async () => done({ accepted: true, power_state: null }))
 expect(await screen.findByRole('status')).toHaveTextContent('physical confirmation pending')
})
it('keeps the last measured state on an unconfirmed request without private error logging', async () => {
 state.read.mockResolvedValue({ power_state: true })
 state.off.mockRejectedValue(new Error('Synthetic private marker'))
 const log = vi.spyOn(console, 'error').mockImplementation(() => {})
 try {
  page()
  fireEvent.click(await screen.findByRole('button', { name: 'Power off plug' }))
  expect(await screen.findByRole('status')).toHaveTextContent('Power command not confirmed')
  expect(screen.getByRole('button', { name: 'Power off plug' })).toBeInTheDocument()
  expect(log).not.toHaveBeenCalled()
 } finally { log.mockRestore() }
})
it.each(['principal', 'organization', 'unmount'])('discards a retired completion after %s changes', async change => {
 let done: any
 state.on.mockImplementation(() => new Promise(resolve => { done = resolve }))
 const view = page()
 fireEvent.click(await screen.findByRole('button', { name: 'Power on plug' }))
 if (change === 'principal') { state.user = { id: 4, role: 'operator', group_id: 1 }; view.refresh() }
 if (change === 'organization') { state.orgId = 2; view.refresh() }
 if (change === 'unmount') view.unmount()
 await act(async () => done({ accepted: true, power_state: null }))
 expect(screen.queryByText('Command accepted; physical confirmation pending')).not.toBeInTheDocument()
})
it('does not overwrite a command observation with an earlier status request', async () => {
 let read: any
 state.read.mockImplementation(() => new Promise(resolve => { read = resolve }))
 state.on.mockResolvedValue({ power_state: false })
 page()
 fireEvent.click(await screen.findByRole('button', { name: 'Power on plug' }))
 expect(await screen.findByRole('status')).toHaveTextContent('Power state confirmed')
 await act(async () => read({ power_state: true }))
 expect(screen.getByRole('button', { name: 'Power on plug' })).toBeInTheDocument()
})
it('clears retired pending state before returning to the original account', async () => {
 let done: any
 state.on.mockImplementation(() => new Promise(resolve => { done = resolve }))
 const view = page()
 fireEvent.click(await screen.findByRole('button', { name: 'Power on plug' }))
 state.user = { id: 4, role: 'operator', group_id: 1 }; view.refresh()
 await act(async () => done({ accepted: true, power_state: null }))
 state.user = { id: 3, role: 'operator', group_id: 1 }; view.refresh()
 expect(await screen.findByRole('button', { name: 'Power on plug' })).not.toBeDisabled()
})
it('does not overwrite the command receipt with a status read begun during the command', async () => {
 let done: any, read: any
 state.on.mockImplementation(() => new Promise(resolve => { done = resolve }))
 const view = page()
 fireEvent.click(await screen.findByRole('button', { name: 'Power on plug' }))
 state.read.mockImplementation(() => new Promise(resolve => { read = resolve }))
 await act(async () => view.client.setQueryData(['printers', 1], (old: any[]) => old.map(p => ({ ...p, nickname: 'refreshed' }))))
 await waitFor(() => expect(read).toBeTypeOf('function'))
 await act(async () => done({ accepted: true, power_state: null }))
 await act(async () => read({ power_state: true }))
 expect(screen.getByText('State unknown')).toBeInTheDocument()
 expect(screen.queryByRole('button', { name: 'Power off plug' })).not.toBeInTheDocument()
})
