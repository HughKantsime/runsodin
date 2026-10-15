import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Spools from './Spools'
import QRScannerModal from '../../components/inventory/QRScannerModal'
const api = vi.hoisted(() => ({ list: vi.fn(), lookup: vi.fn(), assign: vi.fn(), printers: vi.fn(), decoder: vi.fn(), camera: vi.fn(), stop: vi.fn() }))
vi.mock('../../api', () => ({ spools: { list: api.list, lookup: api.lookup, scanAssign: api.assign }, printers: { list: api.printers }, filaments: { list: vi.fn().mockResolvedValue([]) }, bulkOps: { spools: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 17 }) }))
// Decoder/image capture are external boundaries: frame→lookup flow, not actual QR decoding.
vi.mock('jsqr', () => ({ default: api.decoder }))
const spool = { id: 1, qr_code: 'SPL-ACCEPTANCE', status: 'active', filament_brand: 'Fixture', filament_name: 'Low spool', filament_material: 'PLA', filament_color_hex: 'FF0000', initial_weight_g: 1000, remaining_weight_g: 100, percent_remaining: 10, location_printer_id: null, pa_profile: 'K0.042', is_low_stock: false }
const data = [spool, { ...spool, id: 2, qr_code: 'SPL-SECOND', filament_name: 'Loaded spool', remaining_weight_g: 600, percent_remaining: 60, location_printer_id: 5, location_slot: 2 }, { ...spool, id: 3, qr_code: 'SPL-ARCHIVED', filament_name: 'Archived spool', remaining_weight_g: 200, percent_remaining: 20, status: 'archived' }]
const printer = { id: 5, name: 'Acceptance AMS', is_active: true, slot_count: 4, filament_slots: [] }
const clients: QueryClient[] = []
function show(node: React.ReactNode) { const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c); return render(<QueryClientProvider client={c}><MemoryRouter>{node}</MemoryRouter></QueryClientProvider>) }
async function inventory() { show(<Spools />); await screen.findByText('SPL-ACCEPTANCE') }
function card(label: string) { return screen.getByText(label, { exact: true }).parentElement!.parentElement! }
async function manualCode(code = ' acceptance ', onAssigned = vi.fn()) {
  show(<QRScannerModal isOpen onClose={vi.fn()} onAssigned={onAssigned} />)
  fireEvent.click(screen.getByRole('button', { name: 'Enter code manually →' }))
  fireEvent.change(screen.getByPlaceholderText('SPL-XXXXXXXX'), { target: { value: code } }); fireEvent.click(screen.getByRole('button', { name: 'Look Up' }))
  await screen.findByText('SPL-ACCEPTANCE')
}
function selectSlot(slot = '3') { fireEvent.change(screen.getByRole('combobox'), { target: { value: '5' } }); fireEvent.click(screen.getByRole('button', { name: slot, exact: true })) }
beforeEach(() => {
  vi.clearAllMocks(); api.list.mockImplementation(async ({ status }) => status ? data.filter(s => s.status === status) : data)
  api.lookup.mockResolvedValue(spool); api.printers.mockResolvedValue([printer, { ...printer, id: 6, name: 'Inactive printer', is_active: false }]); api.assign.mockResolvedValue({ success: true, message: 'Assigned acceptance spool' })
  api.camera.mockResolvedValue({ getTracks: () => [{ stop: api.stop }] }); api.decoder.mockReturnValue(null)
  vi.stubGlobal('navigator', Object.assign(Object.create(navigator), { mediaDevices: { getUserMedia: api.camera } }))
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
  vi.spyOn(HTMLVideoElement.prototype, 'videoWidth', 'get').mockReturnValue(1); vi.spyOn(HTMLVideoElement.prototype, 'videoHeight', 'get').mockReturnValue(1)
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: vi.fn(), getImageData: () => ({ data: new Uint8ClampedArray(4), width: 1, height: 1 }) } as any)
})
afterEach(() => { cleanup(); clients.forEach(c => c.clear()); clients.length = 0; vi.restoreAllMocks(); vi.unstubAllGlobals() })
it('F06-6.1-02 renders active count, remaining weight, loaded count and actionable low warning', async () => {
  await inventory(); expect(card('Active Spools')).toHaveTextContent('2'); expect(card('Loaded')).toHaveTextContent('1'); expect(card('Total Filament')).toHaveTextContent('700g'); expect(card('Low (<20%)')).toHaveTextContent('1')
  expect(api.list).toHaveBeenCalledWith({ status: 'active', org_id: 17 }); fireEvent.click(screen.getByRole('button', { name: '1 spool running low on filament' }))
  const sort = screen.getAllByRole('combobox').find(el => el.querySelector('option[value="remaining"]'))!; expect(sort).toHaveValue('remaining')
})
it('F06-6.1-02 CLAIM GAP: total spool stat includes archived inventory when viewing All', async () => {
  await inventory(); fireEvent.click(screen.getByRole('button', { name: 'All', exact: true })); await screen.findByText('SPL-ARCHIVED'); expect(card('Total Spools')).toHaveTextContent('3')
})
it('F06-6.2-02 renders individual spool label endpoint link', async () => {
  await inventory(); const links = screen.getAllByRole('link', { name: 'Label' }); expect(links.map(a => a.getAttribute('href'))).toEqual(expect.arrayContaining(['/api/spools/1/label','/api/spools/2/label']))
  expect(links[0]).toHaveAttribute('target', '_blank'); expect(links[0]).toHaveAttribute('rel', 'noopener noreferrer')
})
it('F06-6.2-02 CLAIM GAP: selected spools provide a batch label print action', async () => {
  await inventory(); fireEvent.click(screen.getByRole('checkbox', { name: 'Select all' })); expect(screen.getByText('2 selected')).toBeInTheDocument(); expect(screen.getByRole('button', { name: /(?:batch|print).*labels/i })).toBeInTheDocument()
})
it('F06-6.2-03 manual entry normalizes case/whitespace/prefix', async () => {
  await manualCode(); expect(api.lookup).toHaveBeenCalledWith('SPL-ACCEPTANCE'); expect(api.camera).toHaveBeenCalledWith({ video: { facingMode: 'environment' } })
})
it('F06-6.2-03 keyboard scanner style code+Enter preserves typing focus then invokes lookup', async () => {
  show(<QRScannerModal isOpen onClose={vi.fn()} />); fireEvent.click(screen.getByRole('button', { name: 'Enter code manually →' })); const input = screen.getByPlaceholderText('SPL-XXXXXXXX')
  await userEvent.type(input, 'SPL-ACCEPTANCE'); expect(input).toHaveFocus(); expect(input).toHaveValue('SPL-ACCEPTANCE'); await userEvent.keyboard('{Enter}'); await waitFor(() => expect(api.lookup).toHaveBeenCalledWith('SPL-ACCEPTANCE')); expect(await screen.findByText('SPL-ACCEPTANCE')).toBeInTheDocument()
})
it('F06-6.2-03 camera frame decoder result triggers spool lookup and stops media', async () => {
  api.decoder.mockReturnValue({ data: 'SPL-ACCEPTANCE' }); show(<QRScannerModal isOpen onClose={vi.fn()} />); await screen.findByText('SPL-ACCEPTANCE')
  expect(api.decoder).toHaveBeenCalled(); expect(api.lookup).toHaveBeenCalledWith('SPL-ACCEPTANCE'); expect(api.stop).toHaveBeenCalled()
})
it('F06-6.2-03 camera rejection shows manual fallback without claiming a successful scan', async () => {
  api.camera.mockRejectedValue(new Error('Permission denied')); vi.spyOn(console, 'error').mockImplementation(() => {}); show(<QRScannerModal isOpen onClose={vi.fn()} />)
  expect(await screen.findByText('Could not access camera. Use manual entry instead.')).toBeInTheDocument(); expect(screen.getByPlaceholderText('SPL-XXXXXXXX')).toBeInTheDocument(); expect(api.lookup).not.toHaveBeenCalled()
})
it('F06-6.2-04 scanning reveals printer/slot assignment and submits exact identifiers', async () => {
  const onAssigned = vi.fn(); await manualCode('acceptance', onAssigned); const assign = screen.getByRole('button', { name: 'Assign to Slot' }); expect(assign).toBeDisabled(); expect(screen.queryByRole('option', { name: 'Inactive printer' })).not.toBeInTheDocument()
  fireEvent.change(screen.getByRole('combobox'), { target: { value: '5' } }); expect(assign).toBeDisabled(); for (const n of ['1','2','3','4']) expect(screen.getByRole('button', { name: n, exact: true })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '3', exact: true })); fireEvent.click(assign); await screen.findByText('Assigned acceptance spool'); expect(api.assign).toHaveBeenCalledWith('SPL-ACCEPTANCE', 5, 3); expect(onAssigned).toHaveBeenCalledWith({ success: true, message: 'Assigned acceptance spool' })
})
it('F06-6.2-04 assignment rejection remains visible and does not signal completion', async () => {
  api.assign.mockRejectedValue(new Error('Offline')); await manualCode(); selectSlot('1'); fireEvent.click(screen.getByRole('button', { name: 'Assign to Slot' }))
  expect(await screen.findByText('Failed to assign spool')).toBeInTheDocument(); expect(screen.queryByText('Assigned acceptance spool')).not.toBeInTheDocument()
})
it('F06-6.2-04 CLAIM GAP: assignment dialog preserves measured zero remaining weight', async () => {
  api.lookup.mockResolvedValue({ ...spool, remaining_weight_g: 0 }); await manualCode(); expect(screen.getByRole('dialog')).toHaveTextContent('Fixture PLA • 0g')
})
it('F06-6.3-02 CLAIM GAP: spool list displays stored PA profile', async () => {
  await inventory(); expect(screen.getAllByText('K0.042').length).toBeGreaterThan(0)
})
it('F06-6.3-02 CLAIM GAP: spool detail/edit view displays stored PA profile', async () => {
  await inventory(); fireEvent.click(screen.getAllByTitle('Edit spool')[0]); expect(within(screen.getByRole('dialog', { name: 'Edit Spool' })).getByText('K0.042')).toBeInTheDocument()
})

// F16-16.3-03: the real N shortcut opens the real Add New Spool dialog.
import useKeyboardShortcuts from '../../hooks/useKeyboardShortcuts'
function Shortcuts() { useKeyboardShortcuts(); return null }
it('F16-16.3-03 N on /spools opens the Add New Spool dialog', async () => {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(c)
  c.setQueryData(['auth', 'me'], { id: 3, group_id: 1, role: 'admin' }) // signed-in principal owns the create form
  render(<QueryClientProvider client={c}><MemoryRouter initialEntries={['/spools']}><Shortcuts /><Spools /></MemoryRouter></QueryClientProvider>)
  await screen.findByText('SPL-ACCEPTANCE')
  expect(screen.queryByRole('dialog')).toBeNull()
  fireEvent.keyDown(document, { key: 'n' })
  expect(await screen.findByRole('dialog', { name: 'Add New Spool' })).toBeTruthy()
})
