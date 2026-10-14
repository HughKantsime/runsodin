import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Timelapses from './Timelapses'
import Sidebar from '../../components/layout/Sidebar'
const api = vi.hoisted(() => ({ list: vi.fn(), trim: vi.fn(), speed: vi.fn(), remove: vi.fn(), detail: vi.fn(), check: vi.fn(), edit: vi.fn(), receipt: vi.fn(), printerList: vi.fn() }))
vi.mock('../../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../../api')>()
  return { ...actual, timelapses: { ...actual.timelapses, list: api.list, trim: api.trim, speed: api.speed, delete: api.remove, detail: api.detail, checkMedia: api.check, edit: api.edit, editReceipt: api.receipt, streamUrl: (id: number) => `/api/timelapses/${id}/stream`, downloadUrl: (id: number) => `/api/timelapses/${id}/download` }, printers: { ...actual.printers, list: api.printerList }, stats: { get: vi.fn().mockResolvedValue({}) }, pricingConfig: { get: vi.fn().mockResolvedValue({ ui_mode: 'advanced' }) }, getEducationMode: vi.fn().mockResolvedValue({ enabled: false }) }
})
vi.mock('../../permissions', () => ({ canDo: () => true, canAccessPage: () => true }))
vi.mock('../../BrandingContext', () => ({ useBranding: () => ({ app_name: 'ODIN', app_subtitle: 'Farm' }) }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true }) }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 17, orgs: [], isAdmin: false }) }))
const ready = { id: 31, printer_id: 5, printer_name: 'Ready printer', status: 'ready', duration_seconds: 60, frame_count: 300, file_size_mb: 2, created_at: '2026-10-12T10:00:00', print_job_id: 90 }
const capturing = { ...ready, id: 32, printer_id: 6, printer_name: 'Recording printer', status: 'capturing', frame_count: 12 }
const second = { ...ready, id: 33, printer_name: 'Second page printer' }
const clients: QueryClient[] = []
function show(node: React.ReactNode) { const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); c.setQueryData(['auth', 'me'], { id: 901, username: 'Synthetic gallery admin', role: 'admin', group_id: 17 }); clients.push(c); return render(<QueryClientProvider client={c}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>) }
async function gallery() { const view = show(<Timelapses />); await screen.findByRole('heading', { name: 'Ready printer' }); return view }
function readyCard() { return screen.getByRole('heading', { name: 'Ready printer' }).closest('article') as HTMLElement }
async function editor() { const view = await gallery(); fireEvent.click(within(readyCard()).getByRole('button', { name: 'Edit recording 31' })); await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm trim' })).toBeEnabled()); return view }
beforeEach(() => {
  vi.clearAllMocks(); vi.stubGlobal('__APP_VERSION__', 'audit')
  api.printerList.mockResolvedValue([{ id: 5, name: 'Ready printer' }, { id: 6, name: 'Recording printer' }])
  api.list.mockImplementation(async params => ({ timelapses: params.offset ? [second] : [ready,capturing].filter(t => (!params.printer_id || String(t.printer_id) === params.printer_id) && (!params.status || t.status === params.status)), total: params.printer_id || params.status ? 1 : 25 }))
  api.detail.mockResolvedValue({ row: ready, etag: '"verified-source"' }); api.check.mockResolvedValue(undefined); api.edit.mockImplementation(async (...args) => ({ operation_id: args[4], phase: 'complete', result_tid: 99, result_nonce: 'synthetic-result', frame_count: 300, duration_seconds: 10, playback_available: true })); api.receipt.mockResolvedValue({});
  api.trim.mockResolvedValue(ready); api.speed.mockResolvedValue({ ...ready, id: 99 }); api.remove.mockResolvedValue(undefined)
})
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.restoreAllMocks(); vi.unstubAllGlobals() })
it('F02-2.7-05 renders gallery status, frames, duration, job and admitted download control', async () => {
  await gallery(); const card = readyCard()
  expect(card).toHaveTextContent('ready'); expect(card).toHaveTextContent('300 frames'); expect(card).toHaveTextContent('1m 0s'); expect(card).toHaveTextContent('Job #90')
  expect(within(card).getByRole('button', { name: 'Download recording 31' })).toBeInTheDocument()
  expect(screen.getByText('Recording 32: capturing, 12 frames')).toBeInTheDocument()
})
it('F02-2.7-05 opens ready playback and closes it; capturing has no play action', async () => {
  const view = await gallery(); expect(screen.queryByRole('button', { name: 'Play recording 32' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Play recording 31' }))
  const video = view.container.querySelector('video')!
  expect(video).toHaveAttribute('src', '/api/timelapses/31/stream'); expect(video).toHaveAttribute('controls')
  fireEvent.click(screen.getByRole('button', { name: 'Close player' })); expect(view.container.querySelector('video')).toBeNull()
})
it('F02-2.7-05 filters reset paginated offset', async () => {
  await gallery(); expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Next' })); await screen.findByRole('heading', { name: 'Second page printer' })
  expect(screen.getByText('Page 2 of 2')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Printer filter'), { target: { value: '5' } })
  await waitFor(() => expect(api.list).toHaveBeenCalledWith({ printer_id: '5', status: undefined, limit: 24, offset: 0 }))
  fireEvent.change(screen.getByLabelText('Status filter'), { target: { value: 'ready' } })
  await waitFor(() => expect(api.list).toHaveBeenCalledWith({ printer_id: '5', status: 'ready', limit: 24, offset: 0 }))
})
it('F02-2.7-05 confirms fresh detail and exact lifetime before single deletion', async () => {
  await gallery(); fireEvent.click(screen.getByRole('button', { name: 'Delete recording 31' }))
  const confirm = await screen.findByRole('button', { name: 'Confirm deletion' }); expect(api.remove).not.toHaveBeenCalled()
  expect(api.detail).toHaveBeenCalledWith(31, expect.any(AbortSignal)); fireEvent.click(confirm)
  await waitFor(() => expect(api.remove).toHaveBeenCalledWith(31, '"verified-source"', expect.any(AbortSignal)))
})
it('F02-2.7-05 bulk deletion remains unavailable rather than bypassing lifetime confirmation', async () => {
  await gallery(); expect(screen.queryByRole('checkbox')).toBeNull(); expect(screen.queryByRole('button', { name: 'Delete Selected' })).toBeNull()
  expect(api.remove).not.toHaveBeenCalled()
})
it('F02-2.7-06 main navigation exposes Timelapses with Film icon', async () => {
  show(<Sidebar mobileOpen={false} onMobileClose={vi.fn()} />)
  const nav = screen.getByRole('navigation', { name: 'Main navigation' }); await within(nav).findByRole('link', { name: 'Orders' })
  const link = within(nav).getByRole('link', { name: 'Timelapses' }); expect(link).toHaveAttribute('href', '/timelapses'); expect(link.querySelector('.lucide-film')).not.toBeNull()
})
it('F02-2.7-07 editor uses verified stream, bounded range inputs and six speed presets', async () => {
  const view = await editor(); expect(view.container.querySelector('video')).toHaveAttribute('src', '/api/timelapses/31/stream')
  expect(screen.getByLabelText('Start seconds')).toHaveValue(0); expect(screen.getByLabelText('End seconds (exclusive)')).toHaveValue(60)
  expect(screen.getAllByRole('option').map(x => x.textContent)).toEqual(expect.arrayContaining(['0.5x','1x','1.5x','2x','4x','8x']))
})
it('F02-2.7-07 explicit trim preserves exact range, precondition and operation', async () => {
  await editor(); fireEvent.change(screen.getByLabelText('Start seconds'), { target: { value: '10' } }); fireEvent.change(screen.getByLabelText('End seconds (exclusive)'), { target: { value: '40' } })
  expect(api.edit).not.toHaveBeenCalled(); fireEvent.click(screen.getByRole('button', { name: 'Confirm trim' }))
  await waitFor(() => expect(api.edit).toHaveBeenCalledWith(31, 'trim', { start_seconds: 10, end_seconds: 40 }, '"verified-source"', expect.stringMatching(/^[0-9a-f]{32}$/), expect.any(AbortSignal)))
  await screen.findByText(/Edit completed/)
})
it('F02-2.7-07 prevents zero-length or inverted trim', async () => {
  await editor(); fireEvent.change(screen.getByLabelText('Start seconds'), { target: { value: '60' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm trim' })); expect(screen.getByRole('alert')).toHaveTextContent('valid finite time range')
  fireEvent.change(screen.getByLabelText('End seconds (exclusive)'), { target: { value: '10' } })
  fireEvent.click(screen.getByRole('button', { name: 'Confirm trim' })); expect(screen.getByRole('alert')).toHaveTextContent('valid finite time range'); expect(api.edit).not.toHaveBeenCalled()
})
it.each([0.5,1,1.5,2,4,8])('F02-2.7-07 %sx submits explicit verified speed operation', async multiplier => {
  await editor(); fireEvent.change(screen.getByLabelText('Speed'), { target: { value: String(multiplier) } }); fireEvent.click(screen.getByRole('button', { name: 'Create speed recording' }))
  await waitFor(() => expect(api.edit).toHaveBeenCalledWith(31, 'speed', { multiplier }, '"verified-source"', expect.stringMatching(/^[0-9a-f]{32}$/), expect.any(AbortSignal)))
  await screen.findByText(/Edit completed/)
})
it('F02-2.7-07 browser video metadata does not replace authoritative trim bounds', async () => {
  const view = await editor(); const video = view.container.querySelector('video')!
  Object.defineProperty(video, 'duration', { configurable: true, value: 75 }); fireEvent.loadedMetadata(video)
  expect(screen.getByLabelText('End seconds (exclusive)')).toHaveValue(60)
})
