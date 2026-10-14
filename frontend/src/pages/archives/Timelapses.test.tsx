import { render, screen, fireEvent, waitFor, cleanup, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, afterEach, expect, test, vi } from 'vitest'
import Timelapses from './Timelapses'
const api = vi.hoisted(() => ({ list: vi.fn(), detail: vi.fn(), remove: vi.fn(), check: vi.fn(), edit: vi.fn(), receipt: vi.fn(), allowed: true }))
vi.mock('../../api', () => ({ auth: { me: vi.fn() }, printers: { list: vi.fn().mockResolvedValue([{ id: 9, name: 'Owned printer' }]) }, timelapses: { list: api.list, detail: api.detail, delete: api.remove, checkMedia: api.check, edit: api.edit, editReceipt: api.receipt, streamUrl: (id: number) => `/api/timelapses/${id}/stream`, downloadUrl: (id: number) => `/api/timelapses/${id}/download` } }))
vi.mock('../../permissions', () => ({ canDo: (action: string) => api.allowed && action === 'printers.edit' }))
const row = { id: 17, printer_id: 9, printer_name: 'Owned printer', status: 'ready', frame_count: 3 }
const user = { id: 3, username: 'Owned admin', role: 'admin', group_id: 1 }
function mount(principal: any = user) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); client.setQueryData(['auth', 'me'], principal)
  const view = render(<QueryClientProvider client={client}><MemoryRouter><Timelapses /></MemoryRouter></QueryClientProvider>)
  return { client, ...view }
}
beforeEach(() => { api.allowed = true; api.list.mockResolvedValue({ timelapses: [row], total: 49 }); api.detail.mockResolvedValue({ row, etag: '"exact-authorized-nonce"' }); api.remove.mockResolvedValue(undefined); api.check.mockResolvedValue(undefined) })
afterEach(() => { cleanup(); vi.clearAllMocks() })
test('filters and pagination use bounded list envelope; editor opens only explicitly', async () => {
  mount(); await screen.findByRole('button', { name: 'Play recording 17' })
  expect(screen.getByRole('link', { name: 'Back to Cameras' })).toHaveAttribute('href', '/cameras')
  fireEvent.click(screen.getByRole('button', { name: 'Next' })); await waitFor(() => expect(api.list).toHaveBeenLastCalledWith({ printer_id: undefined, status: undefined, limit: 24, offset: 24 }))
  fireEvent.change(screen.getByLabelText('Status filter'), { target: { value: 'failed' } }); await waitFor(() => expect(api.list).toHaveBeenLastCalledWith({ printer_id: undefined, status: 'failed', limit: 24, offset: 0 }))
  expect(screen.getByRole('button', { name: 'Edit recording 17' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Confirm trim' })).not.toBeInTheDocument()
})
test('authorized detail exact ETag is retained for confirmation and duplicate-click guarded', async () => {
  let finish!: () => void; api.remove.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve }))
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Delete recording 17' }))
  const confirm = await screen.findByRole('button', { name: 'Confirm deletion' }); fireEvent.click(confirm); fireEvent.click(confirm)
  expect(api.detail).toHaveBeenCalledWith(17, expect.any(AbortSignal)); expect(api.remove).toHaveBeenCalledTimes(1); expect(api.remove).toHaveBeenCalledWith(17, '"exact-authorized-nonce"', expect.any(AbortSignal))
  await act(async () => finish()); await waitFor(() => expect(screen.queryByRole('button', { name: 'Confirm deletion' })).not.toBeInTheDocument())
})
test('stale412 refreshes list without retry; next confirmation must load new detail', async () => {
  api.remove.mockRejectedValueOnce(Object.assign(new Error('stale'), { status: 412 }))
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Delete recording 17' })); fireEvent.click(await screen.findByRole('button', { name: 'Confirm deletion' }))
  await screen.findByText(/recording changed/); expect(api.remove).toHaveBeenCalledTimes(1); expect(screen.queryByRole('button', { name: 'Retry deletion' })).not.toBeInTheDocument()
  api.detail.mockResolvedValueOnce({ row, etag: '"new-authorized-nonce"' }); fireEvent.click(screen.getByRole('button', { name: 'Delete recording 17' })); fireEvent.click(await screen.findByRole('button', { name: 'Confirm deletion' }))
  await waitFor(() => expect(api.remove).toHaveBeenLastCalledWith(17, '"new-authorized-nonce"', expect.any(AbortSignal)))
})
test('ambiguous failure keeps original token for explicit retry', async () => {
  api.remove.mockRejectedValueOnce(new Error('lost response'))
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Delete recording 17' })); fireEvent.click(await screen.findByRole('button', { name: 'Confirm deletion' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Retry deletion' })); await waitFor(() => expect(api.remove).toHaveBeenCalledTimes(2))
  expect(api.remove.mock.calls.map(call => call[1])).toEqual(['"exact-authorized-nonce"', '"exact-authorized-nonce"'])
})
test('cache-only account switch removes old rows/player/modal and aborts old pending detail', async () => {
  let finish!: (value: any) => void; api.detail.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const { client } = mount(); fireEvent.click(await screen.findByRole('button', { name: 'Play recording 17' })); expect(screen.getByLabelText('Recording player')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Delete recording 17' })); const signal = api.detail.mock.calls[0][1]
  api.list.mockResolvedValue({ timelapses: [], total: 0 }); client.setQueryData(['auth', 'me'], { ...user, id: 4, username: 'Other user' })
  await waitFor(() => expect(screen.queryByLabelText('Recording player')).not.toBeInTheDocument()); await waitFor(() => expect(signal.aborted).toBe(true))
  await act(async () => finish({ row, etag: '"old"' })); expect(screen.queryByRole('button', { name: 'Confirm deletion' })).not.toBeInTheDocument(); expect(api.remove).not.toHaveBeenCalled()
  await waitFor(() => expect(screen.queryByText('Recording 17: ready, 3 frames')).not.toBeInTheDocument())
})
test('viewer cannot delete and a refused playback gives useful provenance/retry message', async () => {
  const { container } = mount({ ...user, role: 'viewer' }); fireEvent.click(await screen.findByRole('button', { name: 'Play recording 17' }))
  expect(screen.queryByRole('button', { name: 'Delete recording 17' })).not.toBeInTheDocument(); const old = container.querySelector('video')!; fireEvent.error(old)
  expect(screen.getByRole('alert')).toHaveTextContent('verified lifetime provenance'); fireEvent.click(screen.getByRole('button', { name: 'Retry playback' })); expect(container.querySelector('video')).not.toBe(old)
})
test('unknown lifetime detail refusal remains visible and never sends deletion', async () => {
  api.detail.mockRejectedValueOnce(new Error('This recording has no verified lifetime precondition. Deletion is unavailable.'))
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Delete recording 17' })); await screen.findByText(/no verified lifetime precondition/); expect(api.remove).not.toHaveBeenCalled()
})
test('list errors expose manual retry', async () => {
  api.list.mockRejectedValueOnce(new Error('unavailable')); mount(); fireEvent.click(await screen.findByRole('button', { name: 'Retry list' })); await screen.findByRole('button', { name: 'Play recording 17' })
})

test('download admission errors are visible and never initiate a download', async () => {
  api.check.mockRejectedValueOnce(new Error('Media unavailable (503). Historical recordings without verified lifetime provenance are refused.'))
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Download recording 17' })); await screen.findByText(/Media unavailable/)
  expect(click).not.toHaveBeenCalled(); click.mockRestore()
})
test('cache-only owner switch aborts download preflight and ignores old success', async () => {
  let finish!: () => void; api.check.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve }))
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  const { client } = mount(); fireEvent.click(await screen.findByRole('button', { name: 'Download recording 17' }))
  const signal = api.check.mock.calls[0][1]; api.list.mockResolvedValue({ timelapses: [], total: 0 }); client.setQueryData(['auth', 'me'], { ...user, id: 4 })
  await waitFor(() => expect(signal.aborted).toBe(true)); await act(async () => finish()); expect(click).not.toHaveBeenCalled(); click.mockRestore()
})
test('old detail failure after cancelling into playback cannot overwrite new selection', async () => {
  let fail!: (err: Error) => void; api.detail.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject }))
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Delete recording 17' })); fireEvent.click(screen.getByRole('button', { name: 'Play recording 17' }))
  await act(async () => fail(new Error('old detail error'))); expect(screen.getByLabelText('Recording player')).toBeInTheDocument(); expect(screen.queryByText('old detail error')).not.toBeInTheDocument()
})

test('real page close/reopen after committed lost ACK cannot bypass reload and never resubmits automatically', async () => {
  let operation = ''
  api.edit.mockImplementationOnce(async (_id, _kind, _body, _etag, id) => {
    operation = id
    api.detail.mockResolvedValue({ row: { ...row, duration_seconds: .1 }, etag: '"committed-new-version"' })
    throw new Error('lost ACK after committed edit')
  })
  api.receipt.mockImplementation(async () => ({ operation_id: operation, phase: 'complete', result_tid: 17, result_nonce: 'committed-new-version', frame_count: 3, duration_seconds: .1, playback_available: true }))
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Edit recording 17' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm trim' })).toBeEnabled())
  fireEvent.change(screen.getByLabelText('End seconds (exclusive)'), { target: { value: '.1' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm trim' })); await screen.findByText(/no automatic retry/)
  fireEvent.click(screen.getByRole('button', { name: 'Close editor' }))
  fireEvent.click(screen.getByRole('button', { name: 'Edit recording 17' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm trim' })).toBeDisabled())
  expect(api.edit).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: 'Reload and review' }))
  await screen.findByText(/Edit completed/); await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm trim' })).toBeEnabled())
  expect(api.edit).toHaveBeenCalledTimes(1)
  expect(api.receipt).toHaveBeenCalledWith(17, operation, expect.any(AbortSignal))
})
