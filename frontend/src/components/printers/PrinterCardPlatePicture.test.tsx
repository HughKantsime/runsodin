import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render as rtlRender, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import PrinterCard from './PrinterCard'

vi.mock('../../permissions', () => ({ canDo: () => false, getCurrentUser: () => null }))

const render = (ui: ReactElement) => rtlRender(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>)
const URL_PATH = '/api/printers/199/current-plate-thumbnail'
const fetchMock = vi.fn()
const imageResponse = () => new Response(new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' }), { status: 200, headers: { 'Content-Type': 'image/png' } })
const plateCalls = () => fetchMock.mock.calls.filter(([url]) => String(url) === URL_PATH)

const printer = {
  id: 199, name: 'Synthetic printing printer', nickname: null, model: null, is_active: true, api_type: null, api_host: null,
  has_api_key: false, camera_url: null, bed_temp: null, bed_target_temp: null, nozzle_temp: null, nozzle_target_temp: null,
  gcode_state: 'RUNNING', print_stage: 'Printing', lights_on: null, filament_slots: [], tags: [], machine_type: null,
  last_seen: new Date().toISOString(), gcode_file: 'first.gcode', current_print_display_name: 'First',
}
const props = { allFilaments: [], spools: [], onDelete: vi.fn(), onToggleActive: vi.fn(), onUpdateSlot: vi.fn(), onEdit: vi.fn(), onSyncAms: vi.fn() }

beforeEach(() => {
  fetchMock.mockReset()
  fetchMock.mockImplementation(async (url: string) => (String(url) === URL_PATH ? imageResponse() : new Response('{}', { status: 404 })))
  vi.stubGlobal('fetch', fetchMock)
  URL.createObjectURL = vi.fn(() => 'blob:plate-1')
  URL.revokeObjectURL = vi.fn()
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('is off by default: no request and nothing rendered', async () => {
  render(<PrinterCard printer={printer} {...props} />)
  await Promise.resolve()
  expect(plateCalls()).toHaveLength(0)
  expect(screen.queryByTestId('plate-picture')).not.toBeInTheDocument()
})

it('shows the plate picture of an active print when the preference is on', async () => {
  render(<PrinterCard printer={printer} {...props} showPlatePicture />)
  const image = await screen.findByTestId('plate-picture')
  expect(image).toHaveAttribute('src', 'blob:plate-1')
  expect(image).toHaveAttribute('width', '56')
  expect(plateCalls()).toHaveLength(1)
  expect(plateCalls()[0][1]).toEqual(expect.objectContaining({ credentials: 'include' }))
})

it('renders nothing when the endpoint answers 404', async () => {
  fetchMock.mockImplementation(async () => new Response('{"detail":"Plate picture not available"}', { status: 404 }))
  render(<PrinterCard printer={printer} {...props} showPlatePicture />)
  await waitFor(() => expect(plateCalls()).toHaveLength(1))
  await Promise.resolve()
  expect(screen.queryByTestId('plate-picture')).not.toBeInTheDocument()
})

it('requests nothing for a printer that is not printing', async () => {
  render(<PrinterCard printer={{ ...printer, gcode_state: 'IDLE', print_stage: 'Idle' }} {...props} showPlatePicture />)
  await Promise.resolve()
  expect(plateCalls()).toHaveLength(0)
  expect(screen.queryByTestId('plate-picture')).not.toBeInTheDocument()
})

it('refetches when the running print changes and revokes the replaced object URL', async () => {
  vi.mocked(URL.createObjectURL).mockReturnValueOnce('blob:first').mockReturnValueOnce('blob:second')
  const view = render(<PrinterCard printer={printer} {...props} showPlatePicture />)
  expect(await screen.findByTestId('plate-picture')).toHaveAttribute('src', 'blob:first')
  view.rerender(<QueryClientProvider client={new QueryClient()}><PrinterCard printer={{ ...printer, gcode_file: 'second.gcode', current_print_display_name: 'Second' }} {...props} showPlatePicture /></QueryClientProvider>)
  await waitFor(() => expect(screen.getByTestId('plate-picture')).toHaveAttribute('src', 'blob:second'))
  expect(plateCalls()).toHaveLength(2)
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:first')
})

it('revokes the object URL on unmount', async () => {
  const view = render(<PrinterCard printer={printer} {...props} showPlatePicture />)
  await screen.findByTestId('plate-picture')
  view.unmount()
  expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:plate-1')
})
