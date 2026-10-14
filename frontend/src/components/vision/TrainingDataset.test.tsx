import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { webcrypto, createHash } from 'node:crypto'
import Dataset from './TrainingDataset'
import BoundedVisionImage from './BoundedVisionImage'
import { maintenanceAudience } from './maintenanceMedia'
import { imageBudget } from './maintenanceMedia'
import { ownedJpeg } from './ownedJpeg.test-fixture'
const api = vi.hoisted(() => ({ list: vi.fn(), get: vi.fn(), save: vi.fn(), allowed: true }))
vi.mock('../../api', () => ({ auth: { me: vi.fn() }, training: { getFrame: (...a: unknown[]) => api.get(...a), list: (...a: unknown[]) => api.list(...a), annotate: (...a: unknown[]) => api.save(...a) } }))
vi.mock('../../permissions', () => ({ canDo: (action: string) => action === "printers.edit" && api.allowed }))
const principal = { id: 3, username: 'owned', role: 'admin', group_id: null }
const row = { id: 701, printer_id: 9, frame_path: '9/frame.jpg', detection_type: 'spaghetti', status: 'confirmed', annotation: null, ownership: 'unknown', maintenance_only: true }
let number = 0; const OriginalURL = URL
function deferred() { let resolve!: (value: any) => void; let reject!: (value: Error) => void; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
function mount(user: any = principal, initialId?: number) { const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); client.setQueryData(['auth', 'me'], user); render(<QueryClientProvider client={client}><Dataset initialId={initialId} /></QueryClientProvider>); return client }
beforeEach(() => { number = 0; api.allowed = true; api.list.mockReset().mockResolvedValue([row]); api.get.mockReset().mockResolvedValue(row); api.save.mockReset().mockResolvedValue({ annotation_revision: 1 }); vi.stubGlobal('crypto', webcrypto); vi.stubGlobal('URL', class extends OriginalURL { static createObjectURL = vi.fn(() => 'blob:owned-' + ++number); static revokeObjectURL = vi.fn() }); vi.stubGlobal('fetch', vi.fn(async () => new Response(ownedJpeg, { headers: { 'content-type': 'image/jpeg' } }))) })
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); expect([imageBudget.encoded, imageBudget.decoded, imageBudget.working]).toEqual([0, 0, 0]) })
async function editor() { fireEvent.click(await screen.findByRole('button', { name: 'Preview and label' })); await screen.findByRole('img', { name: 'Annotation preview' }) }
it('grid/list show same metadata and explicit negative saves exact viewed hash without borrowing inference boxes', async () => {
  mount(); await screen.findByText(/Detection 701/); fireEvent.click(screen.getByRole('button', { name: 'Dataset list' })); expect(screen.getByText(/Detection 701/)).toBeInTheDocument(); await editor()
  fireEvent.change(screen.getByLabelText('Human class'), { target: { value: 'false_positive' } }); fireEvent.click(screen.getByRole('button', { name: 'Save human annotation' })); await waitFor(() => expect(api.save).toHaveBeenCalledTimes(1))
  expect(api.save).toHaveBeenCalledWith(701, { expected_revision: 0, expected_image_sha256: createHash('sha256').update(ownedJpeg).digest('hex'), class: 'false_positive', negative: true, bbox: [] }); expect(row.detection_type).toBe('spaghetti')
})
it('positive manual coordinates and synchronous pending guard preserve one immutable save', async () => {
  const result = deferred(); api.save.mockReturnValue(result.promise); mount(); await editor(); fireEvent.change(screen.getByLabelText('Human class'), { target: { value: 'detachment' } })
  for (const [name, value] of [['Left', '8'], ['Top', '8'], ['Right', '24'], ['Bottom', '24']]) fireEvent.change(screen.getByLabelText(name), { target: { value } })
  const button = screen.getByRole('button', { name: 'Save human annotation' }); act(() => { fireEvent.click(button); fireEvent.click(button) }); expect(api.save).toHaveBeenCalledTimes(1); expect(api.save.mock.calls[0][1].bbox).toEqual([8, 8, 24, 24]); await act(async () => result.resolve({ annotation_revision: 1 }))
})
it('ambiguous persistence blocks retry until authoritative reload succeeds', async () => {
  const reload = deferred(); api.get.mockReturnValue(reload.promise); api.save.mockRejectedValue(new Error('Response lost')); mount(); await editor(); fireEvent.change(screen.getByLabelText('Human class'), { target: { value: 'false_positive' } }); fireEvent.click(screen.getByRole('button', { name: 'Save human annotation' })); await waitFor(() => expect(screen.getByRole('button', { name: 'Save human annotation' })).toBeDisabled()); expect(api.save).toHaveBeenCalledTimes(1); await act(async () => reload.resolve(row)); await screen.findByRole('img', { name: 'Annotation preview' })
})
it('cache-only switch retires editor and all URL resources; stale save cannot affect new audience', async () => {
  const result = deferred(); api.save.mockReturnValue(result.promise); const client = mount(); await editor(); const url = screen.getByRole('img', { name: 'Annotation preview' }).getAttribute('src'); fireEvent.change(screen.getByLabelText('Human class'), { target: { value: 'false_positive' } }); fireEvent.click(screen.getByRole('button', { name: 'Save human annotation' })); await act(async () => client.setQueryData(['auth', 'me'], { id: 4, username: 'other', role: 'viewer', group_id: 1 })); await waitFor(() => expect(screen.queryByRole('img', { name: 'Annotation preview' })).not.toBeInTheDocument()); expect(URL.revokeObjectURL).toHaveBeenCalledWith(url); await act(async () => result.resolve({ annotation_revision: 1 })); expect(screen.getByText(/platform maintenance only/)).toBeInTheDocument(); expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
it.each([undefined, 0, 1])('requires exact null organization, refusing %s before dataset request', group_id => { mount({ ...principal, group_id }); expect(api.list).not.toHaveBeenCalled(); expect(fetch).not.toHaveBeenCalled() })
it('export checks ZIP and current cache before download and always cleans the URL', async () => {
  vi.stubGlobal('fetch', vi.fn(async url => new Response(String(url).includes('/export') ? new Uint8Array([80, 75, 3, 4]) : ownedJpeg, { headers: { 'content-type': String(url).includes('/export') ? 'application/zip' : 'image/jpeg' } }))); const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {}); mount(); fireEvent.click(await screen.findByRole('button', { name: 'Export training ZIP' })); await waitFor(() => expect(click).toHaveBeenCalledTimes(1)); expect(URL.revokeObjectURL).toHaveBeenCalled()
})
it('account switch aborts pending export and late response cannot allocate a download URL', async () => {
  const response = deferred(); let signal: AbortSignal | undefined; vi.stubGlobal('fetch', vi.fn((url, options) => { if (String(url).includes('/export')) { signal = options.signal; return response.promise } return Promise.resolve(new Response(ownedJpeg, { headers: { 'content-type': 'image/jpeg' } })) })); const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {}); const client = mount(); fireEvent.click(await screen.findByRole('button', { name: 'Export training ZIP' })); await act(async () => client.setQueryData(['auth', 'me'], null)); await waitFor(() => expect(signal!.aborted).toBe(true)); await act(async () => response.resolve(new Response(new Uint8Array([80, 75]), { headers: { 'content-type': 'application/zip' } }))); expect(click).not.toHaveBeenCalled()
})

it('30s ZIP acquisition deadline aborts without allocating a download blob', async () => {
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {}); mount(); await screen.findByRole('img', { name: 'Training thumbnail' }); vi.useFakeTimers()
  let signal: AbortSignal | undefined; vi.stubGlobal('fetch', vi.fn((_url, options) => { signal = options.signal; return new Promise((_resolve, reject) => signal!.addEventListener('abort', () => reject(new Error('aborted')))) }))
  fireEvent.click(screen.getByRole('button', { name: 'Export training ZIP' })); await act(async () => vi.advanceTimersByTimeAsync(30_000)); expect(signal!.aborted).toBe(true); expect(click).not.toHaveBeenCalled(); expect(screen.getByRole('alert')).toHaveTextContent('Export timed out')
})
it('offscreen previews retire their URL and reservation rather than retaining image caches', async () => {
  let changed!: (entries: Array<{ isIntersecting: boolean }>) => void
  vi.stubGlobal('IntersectionObserver', class { constructor(callback: typeof changed) { changed = callback } observe() {} disconnect() {} })
  const client = new QueryClient(); client.setQueryData(['auth', 'me'], principal)
  render(<QueryClientProvider client={client}><BoundedVisionImage path="9/frame.jpg" audience={maintenanceAudience(principal as any)} alt="Owned visible preview" /></QueryClientProvider>)
  expect(fetch).not.toHaveBeenCalled(); await act(async () => changed([{ isIntersecting: true }]))
  const image = await screen.findByRole('img', { name: 'Owned visible preview' }); const url = image.getAttribute('src')
  await act(async () => changed([{ isIntersecting: false }])); expect(screen.queryByRole('img')).not.toBeInTheDocument(); expect(URL.revokeObjectURL).toHaveBeenCalledWith(url); expect([imageBudget.encoded, imageBudget.decoded, imageBudget.working]).toEqual([0, 0, 0])
})

it('direct label target outside page0 opens exact authoritative record', async () => {
  api.list.mockResolvedValue([]); api.get.mockResolvedValue({ ...row, id: 999 }); mount(principal, 999)
  await screen.findByRole('img', { name: 'Annotation preview' }); expect(api.get).toHaveBeenCalledWith(999); expect(screen.getByRole('button', { name: 'Save human annotation' })).toBeInTheDocument()
})
it('malformed existing annotation cannot crash the editor or enable writes', async () => {
  api.list.mockResolvedValue([{ ...row, annotation: { revision: 1, class: 'detachment', bbox: 'abcd' } }]); mount(); await editor()
  expect(screen.getByText(/Existing annotation is invalid/)).toBeInTheDocument(); expect(screen.getByRole('button', { name: 'Save human annotation' })).toBeDisabled(); expect(api.save).not.toHaveBeenCalled()
})

it('wrong export MIME aborts the stream and never allocates a download', async () => {
  let signal: AbortSignal | undefined
  vi.stubGlobal('fetch', vi.fn(async (url, options) => { if (String(url).includes('/export')) { signal = options.signal; return new Response('not a dataset', { headers: { 'content-type': 'application/json' } }) } return new Response(ownedJpeg, { headers: { 'content-type': 'image/jpeg' } }) }))
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {}); mount(); fireEvent.click(await screen.findByRole('button', { name: 'Export training ZIP' })); await waitFor(() => expect(signal!.aborted).toBe(true)); expect(click).not.toHaveBeenCalled(); expect(screen.getByRole('alert')).toHaveTextContent('Expected a ZIP')
})
