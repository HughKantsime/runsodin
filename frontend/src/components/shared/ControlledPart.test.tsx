import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import ControlledPart from './ControlledPart'
const state = vi.hoisted(() => ({ enabled: true, role: 'operator', get: vi.fn(), set: vi.fn(), groups: vi.fn() }))
vi.mock('./ControlledAudience', () => ({ default: () => null }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ hasFeature: () => state.enabled }) }))
vi.mock('../../permissions', () => ({ canDo: () => true, getCurrentUser: () => ({ role: state.role }) }))
vi.mock('../../api/controlledParts', () => ({ controlledParts: { get: state.get, set: state.set, groups: state.groups } }))
beforeEach(() => { vi.clearAllMocks(); state.enabled = true; state.role = 'operator'; state.get.mockResolvedValue({ controlled: false, policies: [] }); state.set.mockResolvedValue({ controlled: true }); state.groups.mockResolvedValue([{ id: 1, name: 'Restricted' }]) })
afterEach(cleanup)
function mount() { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><ControlledPart kind="files" id={5} /></QueryClientProvider>) }
test('unlicensed surface makes no calls', () => { state.enabled = false; mount(); expect(state.get).not.toHaveBeenCalled(); expect(screen.queryByText('Controlled part')).toBeNull() })
test('viewer can see marking without an edit action', async () => { state.role = 'viewer'; mount(); expect(await screen.findByText('Unmarked')).toBeTruthy(); expect(screen.queryByText('Change marking')).toBeNull() })
test('operator submits a reason, audience, printer group and retention', async () => {
  mount(); fireEvent.click(await screen.findByText('Change marking')); await screen.findByText('Restricted')
  fireEvent.change(screen.getByLabelText('Controlled audience'), { target: { value: '1' } })
  fireEvent.change(screen.getByLabelText('Allowed printer group tag'), { target: { value: 'Secure' } })
  expect(screen.getByText('Save marking')).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Customer restriction' } })
  fireEvent.click(screen.getByText('Save marking'))
  await waitFor(() => expect(state.set).toHaveBeenCalledWith('files', 5, { controlled: true, label: 'CUI', audience_id: 1, printer_tag: 'Secure', retention_days: 30, reason: 'Customer restriction' }))
  expect(await screen.findByText('Marking saved')).toBeTruthy()
})
test('named refusal stays visible', async () => { state.get.mockRejectedValue(new Error('Controlled part access refused')); mount(); expect(await screen.findByRole('alert')).toHaveTextContent('Controlled part access refused') })
test('superadmin can explicitly acknowledge enabled SIEM destinations when marking', async () => {
  state.role = 'admin'
  mount(); fireEvent.click(await screen.findByText('Change marking')); await screen.findByText('Restricted')
  const ack = screen.getByLabelText(/I acknowledge that enabled SIEM/)
  expect(ack).not.toBeChecked()
  fireEvent.click(ack)
  fireEvent.change(screen.getByLabelText('Controlled audience'), { target: { value: '1' } })
  fireEvent.change(screen.getByLabelText('Allowed printer group tag'), { target: { value: 'Secure' } })
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Restriction' } })
  fireEvent.click(screen.getByText('Save marking'))
  await waitFor(() => expect(state.set).toHaveBeenCalledWith('files', 5, expect.objectContaining({ acknowledge_siem: true })))
})
