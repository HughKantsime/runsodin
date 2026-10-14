import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import TimelapseEditor from './TimelapseEditor'
const api = vi.hoisted(() => ({ detail: vi.fn(), edit: vi.fn(), receipt: vi.fn(), allowed: true }))
vi.mock('../../api', () => ({ auth: { me: vi.fn() }, timelapses: { detail: api.detail, edit: api.edit, editReceipt: api.receipt, streamUrl: (id: number, etag: string) => `/stream/${id}?version=${etag}`, downloadUrl: (id: number, etag: string) => `/download/${id}?version=${etag}` } }))
vi.mock('../../permissions', () => ({ canDo: () => api.allowed }))
const user = { id: 3, username: 'owner', role: 'operator', group_id: 1 }
const row: any = { id: 17, status: 'ready', duration_seconds: 1 }
const result: any = { operation_id: 'a'.repeat(32), phase: 'complete', result_tid: 18, result_nonce: 'new', frame_count: 30, duration_seconds: 1, playback_available: true }
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); client.setQueryData(['auth', 'me'], user)
  const done = vi.fn(), close = vi.fn()
  const ui = (selected: any) => <QueryClientProvider client={client}><TimelapseEditor row={selected} onClose={close} onComplete={done} /></QueryClientProvider>
  const view = render(ui(row)); return { client, done, close, ...view, selected: (value: any) => view.rerender(ui(value)) }
}
beforeEach(() => { api.allowed = true; api.detail.mockResolvedValue({ row, etag: '"source"' }); api.edit.mockImplementation(async (...args) => ({ ...result, operation_id: args[4] })); api.receipt.mockResolvedValue(result) })
afterEach(() => { cleanup(); vi.clearAllMocks() })
test('range and all six speeds use exact verified precondition and explicit operation', async () => {
  mount(); await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm trim' })).toBeEnabled())
  expect(screen.getAllByRole('option').map(el => el.textContent)).toEqual(['0.5x','1x','1.5x','2x','4x','8x'])
  fireEvent.change(screen.getByLabelText('Start seconds'), { target: { value: '.04' } }); fireEvent.change(screen.getByLabelText('End seconds (exclusive)'), { target: { value: '.2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm trim' }))
  await waitFor(() => expect(api.edit).toHaveBeenCalledTimes(1))
  expect(api.edit).toHaveBeenCalledWith(17, 'trim', { start_seconds: .04, end_seconds: .2 }, '"source"', expect.stringMatching(/^[0-9a-f]{32}$/), expect.any(AbortSignal))
  await screen.findByText(/Edit completed/)
})
test('lost ACK survives close/reopen and requires authoritative receipt reload', async () => {
  api.edit.mockRejectedValueOnce(new Error('lost ACK after commit')); const view = mount()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm trim' })).toBeEnabled()); fireEvent.click(screen.getByRole('button', { name: 'Confirm trim' }))
  await screen.findByText(/no automatic retry/); const operation = api.edit.mock.calls[0][4]
  view.selected(null); view.selected(row)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm trim' })).toBeDisabled())
  expect(api.edit).toHaveBeenCalledTimes(1)
  api.receipt.mockResolvedValueOnce({ ...result, operation_id: operation, playback_available: false })
  fireEvent.click(screen.getByRole('button', { name: 'Reload and review' }))
  await screen.findByText(/no longer current/); await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm trim' })).toBeEnabled())
  expect(api.receipt).toHaveBeenCalledWith(17, operation, expect.any(AbortSignal)); expect(api.edit).toHaveBeenCalledTimes(1)
})
test('cache-only switch retires private inputs and old pending callbacks', async () => {
  let finish!: (value: any) => void; api.edit.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const view = mount(); await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm trim' })).toBeEnabled())
  fireEvent.change(screen.getByLabelText('Start seconds'), { target: { value: '.5' } }); fireEvent.click(screen.getByRole('button', { name: 'Confirm trim' }))
  await act(async () => { view.client.setQueryData(['auth','me'], { ...user, id: 4, username: 'replacement' }) })
  await waitFor(() => expect(screen.getByLabelText('Start seconds')).toHaveValue(0))
  await act(async () => finish({ ...result, operation_id: api.edit.mock.calls[0][4] }))
  expect(view.done).not.toHaveBeenCalled(); expect(screen.queryByText(/Edit completed/)).not.toBeInTheDocument()
})
test('pending duplicate submission is suppressed and viewer has no editor', async () => {
  api.edit.mockImplementationOnce(() => new Promise(() => {})); const view = mount()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Create speed recording' })).toBeEnabled())
  const button = screen.getByRole('button', { name: 'Create speed recording' }); fireEvent.click(button); fireEvent.click(button)
  expect(api.edit).toHaveBeenCalledTimes(1)
  await act(async () => { view.client.setQueryData(['auth','me'], { ...user, role: 'viewer' }) })
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
})
test('modal range sliders are bounded by the recording and drive the trim request', async () => {
  api.detail.mockResolvedValue({ row: { ...row, duration_seconds: 10 }, etag: '"source"' })
  mount(); await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm trim' })).toBeEnabled())
  expect(screen.getByRole('dialog', { name: 'Edit recording' })).toHaveAttribute('aria-modal', 'true')
  const startSlider = screen.getByRole('slider', { name: 'Trim start' }), endSlider = screen.getByRole('slider', { name: 'Trim end' })
  expect(startSlider).toHaveAttribute('max', '10'); expect(endSlider).toHaveAttribute('max', '10')
  fireEvent.change(startSlider, { target: { value: '2.5' } }); fireEvent.change(endSlider, { target: { value: '7' } })
  expect(screen.getByLabelText('Start seconds')).toHaveValue(2.5); expect(screen.getByLabelText('End seconds (exclusive)')).toHaveValue(7)
  // Dragging end below start keeps a valid, non-empty range.
  fireEvent.change(endSlider, { target: { value: '2' } })
  expect(Number((screen.getByLabelText('Start seconds') as HTMLInputElement).value)).toBeLessThan(2)
  fireEvent.change(endSlider, { target: { value: '7' } }); fireEvent.change(startSlider, { target: { value: '2.5' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm trim' }))
  await waitFor(() => expect(api.edit).toHaveBeenCalledWith(17, 'trim', { start_seconds: 2.5, end_seconds: 7 }, '"source"', expect.stringMatching(/^[0-9a-f]{32}$/), expect.any(AbortSignal)))
})
