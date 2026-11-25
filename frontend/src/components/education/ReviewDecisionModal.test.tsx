import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ReviewDecisionModal from './ReviewDecisionModal'
import { education } from '../../api/education'
import type { EducationSubmission } from '../../types'
const api = vi.hoisted(() => ({ listCenterPrinters: vi.fn(), previewCompatibility: vi.fn(), readAMS: vi.fn(), approveSubmission: vi.fn(), rejectSubmission: vi.fn() }))
vi.mock('../../api', () => ({ education: api }))
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn() } }))
const submission = { id: 5, job_id: 6, cost_center_id: 1, submitted_by: 2, submitter_username: 'student', item_name: 'Coupon', status: 'submitted', approved_printer_id: null, lifecycle_revision: 1, compatibility_engine_version: null, rejection_reason: null, created_at: null, updated_at: null } satisfies EducationSubmission
const report = { generation_id: 'a'.repeat(32), packet_sequence: 4, received_at: '2026-10-12T10:00:00Z', trays: [{ slot_number: 1, material: 'PETG', material_state: 'reported', mechanically_ready: null }] }
let client: QueryClient
beforeEach(() => {
  vi.clearAllMocks()
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  api.listCenterPrinters.mockResolvedValue({ items: [{ printer_id: 9, name: 'A1', api_type: 'bambu', machine_type: 'A1' }, { printer_id: 10, name: 'Other', api_type: 'moonraker', machine_type: 'Other' }] })
  api.previewCompatibility.mockResolvedValue({ compatible: true, reasons: [] })
  api.readAMS.mockResolvedValue(report)
  api.approveSubmission.mockImplementation(async (id, body) => ({
    id, status: 'pending', approved_printer_id: body.printer_id,
    ...(body.ams_mapping ? { ams_mapping: [0], ams_generation_id: report.generation_id,
      print_file_id: 4, payload_sha256: 'b'.repeat(64), plate_index: 0 } : {}),
  }))
})
afterEach(() => { cleanup(); client.clear(); vi.unstubAllGlobals() })
function mount() {
  const close = vi.fn()
  render(<QueryClientProvider client={client}><ReviewDecisionModal submission={submission} onClose={close} /></QueryClientProvider>)
  return close
}
async function selectAMS() {
  await screen.findByRole('option', { name: /A1 · A1/ })
  fireEvent.change(screen.getByLabelText('Authorized printer'), { target: { value: '9' } })
  await screen.findByText('File and printer are compatible')
  fireEvent.change(screen.getByLabelText('Filament supply'), { target: { value: 'ams' } })
  fireEvent.change(screen.getByLabelText('Physical AMS tray'), { target: { value: '1' } })
}
it('requires a fresh tray report and sends its exact mapping', async () => {
  const close = mount(); await selectAMS()
  expect(screen.getByRole('button', { name: 'Approve and queue' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Read selected tray' }))
  await screen.findByText(/Tray 1: PETG/)
  expect(api.readAMS).toHaveBeenCalledWith(5, { revision: 1, printer_id: 9, selected_slots: [1] })
  fireEvent.click(screen.getByRole('button', { name: 'Approve and queue' }))
  await waitFor(() => expect(close).toHaveBeenCalled())
  expect(api.approveSubmission).toHaveBeenCalledWith(5, expect.objectContaining({ revision: 1, printer_id: 9, ams_mapping: [{ source_slot: 0, target_slot: 1 }], ams_generation_id: report.generation_id, plate_index: 0 }))
})
it('invalidates evidence when the selected tray changes', async () => {
  mount(); await selectAMS()
  fireEvent.click(screen.getByRole('button', { name: 'Read selected tray' }))
  await screen.findByText(/Tray 1: PETG/)
  fireEvent.change(screen.getByLabelText('Physical AMS tray'), { target: { value: '2' } })
  expect(screen.queryByText(/Current report received/)).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Approve and queue' })).toBeDisabled()
})
it('refuses approval on read failure and unknown material', async () => {
  api.readAMS.mockRejectedValueOnce(new Error('Tray unavailable'))
  mount(); await selectAMS()
  fireEvent.click(screen.getByRole('button', { name: 'Read selected tray' }))
  await screen.findByText('Tray unavailable')
  expect(screen.getByRole('button', { name: 'Approve and queue' })).toBeDisabled()
  api.readAMS.mockResolvedValueOnce({ ...report, trays: [{ slot_number: 1, material: null, material_state: 'unknown', mechanically_ready: null }] })
  fireEvent.click(screen.getByRole('button', { name: 'Read selected tray' }))
  await screen.findByText(/Unknown material/)
  expect(screen.getByRole('button', { name: 'Approve and queue' })).toBeDisabled()
  expect(api.approveSubmission).not.toHaveBeenCalled()
})
it('replays the same command after an uncertain approval response', async () => {
  api.approveSubmission.mockRejectedValueOnce(new Error('Approval result unconfirmed'))
  mount(); await selectAMS()
  fireEvent.click(screen.getByRole('button', { name: 'Read selected tray' }))
  await screen.findByText(/Tray 1: PETG/)
  fireEvent.click(screen.getByRole('button', { name: 'Approve and queue' }))
  await screen.findByText('Approval result unconfirmed')
  expect(screen.getByText(/Retry uses the same approval request/)).toBeVisible()
  expect(screen.getByLabelText('Authorized printer')).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Read selected tray' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Retry approval' }))
  await waitFor(() => expect(api.approveSubmission).toHaveBeenCalledTimes(2))
  expect(api.approveSubmission.mock.calls[1]).toEqual(api.approveSubmission.mock.calls[0])
})
it('preserves ordinary non-Bambu approval without AMS fields', async () => {
  mount(); await screen.findByRole('option', { name: /Other · Other/ })
  fireEvent.change(screen.getByLabelText('Authorized printer'), { target: { value: '10' } })
  await screen.findByText('File and printer are compatible')
  fireEvent.click(screen.getByRole('button', { name: 'Approve and queue' }))
  await waitFor(() => expect(api.approveSubmission).toHaveBeenCalledOnce())
  expect(api.approveSubmission.mock.calls[0][1]).not.toHaveProperty('ams_mapping')
})

it.each([
  { status: 429, code: 'quota_exceeded', retriable: false, locked: false, detail: 'This student has used up their print quota for this period.' },
  { status: 503, code: 'upstream_unavailable', retriable: true, locked: true, detail: 'Printer temporarily unavailable.' },
  { status: 503, code: 'resource_conflict', retriable: false, reason: 'approval_unconfirmed', locked: true,
    detail: 'Approval result is unconfirmed. Refresh or replay the same command ID.' },
])('shows the $code refusal under the button and protects unresolved commands', async ({ status, code, retriable, reason, locked, detail }) => {
  const fetch = vi.fn(async (url: string) => {
    if (url === '/api/education/submissions/5/approve') {
      return Response.json({ detail, error: { code, detail, retriable, reason } }, { status })
    }
    if (url.startsWith('/api/education/submissions/5/slice-info')) {
      return Response.json({ plates: [], selected_plate_index: 0, slots: [], freshness: '' })
    }
    throw new Error(`Unexpected request to ${url}`)
  })
  vi.stubGlobal('fetch', fetch)
  api.approveSubmission.mockImplementationOnce(education.approveSubmission)
  const close = mount()
  await selectAMS()
  fireEvent.click(screen.getByRole('button', { name: 'Read selected tray' }))
  await screen.findByText(/Tray 1: PETG/)
  fireEvent.click(screen.getByRole('button', { name: 'Approve and queue' }))
  const approval = within(screen.getByRole('region', { name: 'Approve to a printer' }))
  const alert = await approval.findByRole('alert')
  expect(alert.textContent).toBe(detail)
  const approvalButton = approval.getByRole('button', { name: locked ? 'Retry approval' : 'Approve and queue' })
  expect(alert.previousElementSibling).toBe(approvalButton)
  expect(screen.getAllByText(detail)).toHaveLength(1)
  if (locked) {
    expect(approval.getByText(/Retry uses the same approval request/)).toBeVisible()
  } else {
    expect(screen.queryByText(/Retry uses the same approval request/)).not.toBeInTheDocument()
  }
  expect(fetch).toHaveBeenCalledWith('/api/education/submissions/5/approve', expect.objectContaining({ method: 'POST' }))
  expect(close).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Revise supports' } })
  for (const control of [
    screen.getByLabelText('Authorized printer'), screen.getByLabelText('Filament supply'),
    screen.getByLabelText('Physical AMS tray'), screen.getByRole('button', { name: 'Read selected tray' }),
    screen.getByRole('button', { name: 'Reject submission' }),
  ]) {
    if (locked) expect(control).toBeDisabled()
    else expect(control).toBeEnabled()
  }
  fireEvent.click(approvalButton)
  await waitFor(() => expect(api.approveSubmission).toHaveBeenCalledTimes(2))
  const [first, second] = api.approveSubmission.mock.calls
  if (locked) expect(second).toEqual(first)
  else expect(second[1].command_id).not.toBe(first[1].command_id)
})

it.each(['reject', 'reopen'])('allows %s after a definitive quota refusal', async (action) => {
  api.approveSubmission.mockRejectedValueOnce(Object.assign(new Error('Quota reached'), { retriable: false }))
  const close = vi.fn()
  const dialog = (item: EducationSubmission | null) =>
    <QueryClientProvider client={client}><ReviewDecisionModal submission={item} onClose={close} /></QueryClientProvider>
  const view = render(dialog(submission))
  await screen.findByRole('option', { name: /Other · Other/ })
  fireEvent.change(screen.getByLabelText('Authorized printer'), { target: { value: '10' } })
  await screen.findByText('File and printer are compatible')
  fireEvent.click(screen.getByRole('button', { name: 'Approve and queue' }))
  await screen.findByText('Quota reached')
  if (action === 'reject') {
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Revise supports' } })
    fireEvent.click(screen.getByRole('button', { name: 'Reject submission' }))
    await waitFor(() => expect(api.rejectSubmission).toHaveBeenCalledWith(5, { revision: 1, reason: 'Revise supports', command_id: expect.any(String) }))
    return
  }
  view.rerender(dialog(null))
  view.rerender(dialog(submission))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(screen.getByLabelText('Authorized printer')).toHaveValue('')
  expect(screen.getByRole('button', { name: 'Approve and queue' })).toBeDisabled()
})

it('shows a rejection error only under Reject submission', async () => {
  const detail = 'This submission has already been reviewed.'
  api.rejectSubmission.mockRejectedValueOnce(new Error(detail))
  const close = mount()
  const rejection = within(screen.getByRole('region', { name: 'Return to student' }))
  fireEvent.change(rejection.getByLabelText('Reason'), { target: { value: 'Please check the dimensions.' } })
  const button = rejection.getByRole('button', { name: 'Reject submission' })
  fireEvent.click(button)
  const alert = await rejection.findByRole('alert')
  expect(alert.textContent).toBe(detail)
  expect(alert.previousElementSibling).toBe(button)
  expect(screen.getAllByText(detail)).toHaveLength(1)
  expect(close).not.toHaveBeenCalled()
})

it('ignores a late read after switching submissions', async () => {
  let resolve!: (value: typeof report) => void
  api.readAMS.mockReturnValueOnce(new Promise((done) => { resolve = done }))
  const close = vi.fn()
  const view = render(<QueryClientProvider client={client}><ReviewDecisionModal submission={submission} onClose={close} /></QueryClientProvider>)
  await selectAMS()
  fireEvent.click(screen.getByRole('button', { name: 'Read selected tray' }))
  await waitFor(() => expect(api.readAMS).toHaveBeenCalledOnce())
  view.rerender(<QueryClientProvider client={client}><ReviewDecisionModal submission={{ ...submission, id: 8 }} onClose={close} /></QueryClientProvider>)
  await act(async () => { resolve(report) })
  expect(screen.queryByText(/Current report received/)).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Approve and queue' })).toBeDisabled()
  expect(close).not.toHaveBeenCalled()
})
it('does not close a reopened modal on an old approval response and retains its command', async () => {
  let resolve!: (value: { status: string }) => void
  api.approveSubmission.mockReturnValueOnce(new Promise((done) => { resolve = done }))
  const close = vi.fn()
  const view = render(<QueryClientProvider client={client}><ReviewDecisionModal submission={submission} onClose={close} /></QueryClientProvider>)
  await selectAMS()
  fireEvent.click(screen.getByRole('button', { name: 'Read selected tray' }))
  await screen.findByText(/Tray 1: PETG/)
  fireEvent.click(screen.getByRole('button', { name: 'Approve and queue' }))
  await waitFor(() => expect(api.approveSubmission).toHaveBeenCalledOnce())
  view.rerender(<QueryClientProvider client={client}><ReviewDecisionModal submission={null} onClose={close} /></QueryClientProvider>)
  view.rerender(<QueryClientProvider client={client}><ReviewDecisionModal submission={{ ...submission }} onClose={close} /></QueryClientProvider>)
  await act(async () => { resolve({ id: 5, status: 'pending', approved_printer_id: 9, ams_mapping: [0], ams_generation_id: report.generation_id, print_file_id: 4, payload_sha256: 'b'.repeat(64), plate_index: 0 } as any) })
  expect(close).not.toHaveBeenCalled()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Retry approval' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Retry approval' }))
  await waitFor(() => expect(api.approveSubmission).toHaveBeenCalledTimes(2))
  expect(api.approveSubmission.mock.calls[1]).toEqual(api.approveSubmission.mock.calls[0])
})

it('does not claim approval when the returned mapping differs', async () => {
  api.approveSubmission.mockResolvedValueOnce({ id: 5, approved_printer_id: 9, ams_mapping: [2] })
  const close = mount(); await selectAMS()
  fireEvent.click(screen.getByRole('button', { name: 'Read selected tray' }))
  await screen.findByText(/Tray 1: PETG/)
  fireEvent.click(screen.getByRole('button', { name: 'Approve and queue' }))
  await screen.findByText('Approval result unconfirmed. Retry the same request or refresh the queue.')
  expect(close).not.toHaveBeenCalled()
})
