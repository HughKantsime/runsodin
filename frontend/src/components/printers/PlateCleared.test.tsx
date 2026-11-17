import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import PlateCleared from './PlateCleared'
import ClassicCard from './PrinterCard'
import FoundryCard from '../foundry/PrinterCard'
import PrinterRows from '../foundry/PrinterRows'
import PrinterDetail from '../../pages/printers/PrinterDetail'

const state = vi.hoisted(() => ({ allowed: true, press: vi.fn(), get: vi.fn() }))
vi.mock('../../permissions', () => ({ canDo: () => state.allowed, canAccessPage: () => false, getCurrentUser: () => ({ id: 3, role: state.allowed ? 'operator' : 'viewer' }) }))
vi.mock('../../api/client', () => ({ fetchAPI: vi.fn().mockResolvedValue([]) }))
vi.mock('../../api', async original => ({ ...await original<any>(), printers: { get: state.get, plateCleared: state.press },
  printerTelemetry: { get: vi.fn().mockResolvedValue([]) }, printJobs: { list: vi.fn().mockResolvedValue([]) } }))
vi.mock('./MaintenanceBanner', () => ({ default: () => null }))
vi.mock('./DispatchRecoveryPanel', () => ({ default: () => null }))
vi.mock('./InstalledNozzles', () => ({ default: () => null }))
vi.mock('./StartNextPanel', () => ({ IdleS1StartNext: () => <span>Separate start next</span> }))
const printer: any = { id: 199, name: 'Workshop printer', api_type: 'anycubic_lan', org_id: 1,
  support_level: 'experimental', is_active: true, gcode_state: 'FINISH', filament_slots: [], tags: [],
  plate_clear: { needed: true, has_waiting_job: false, releases_job: null, last_print_ended_at: '2020-01-01T12:00:00Z' } }
const props = { allFilaments: [], spools: [], onDelete: vi.fn(), onToggleActive: vi.fn(), onUpdateSlot: vi.fn(), onEdit: vi.fn(), onSyncAms: vi.fn() }
function setup(node = <PlateCleared printer={printer} />) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return { client, ...render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/printers/199']}>{node}</MemoryRouter></QueryClientProvider>) }
}
function open() { fireEvent.click(screen.getByRole('button', { name: 'Plate cleared' })); return within(screen.getByRole('dialog')) }
beforeEach(() => { state.allowed = true; state.get.mockReset().mockResolvedValue(printer); state.press.mockReset().mockResolvedValue({ success: true, message: 'Plate clear recorded' }) })
afterEach(() => { cleanup(); vi.unstubAllGlobals(); document.documentElement.removeAttribute('data-theme-family') })

it.each([true, false])('shows state and gates button by permission %s', allowed => {
  state.allowed = allowed; setup()
  expect(screen.getByText('Plate not cleared yet')).toBeInTheDocument()
  expect(!!screen.queryByRole('button', { name: 'Plate cleared' })).toBe(allowed)
})
it('shows the confirmed time without a button when clearing is not needed', () => {
  setup(<PlateCleared printer={{ ...printer, plate_clear: { ...printer.plate_clear, needed: false }, plate_clear_confirmed_at: '2020-01-01T13:00:00Z' }} />)
  expect(screen.queryByRole('button', { name: 'Plate cleared' })).not.toBeInTheDocument()
  expect(document.querySelector('time')).toHaveAttribute('dateTime', '2020-01-01T13:00:00Z')
})
it.each(['none', 'named', 'hidden'])('confirms the correct effect for %s', kind => {
  const waiting = kind !== 'none'
  setup(<PlateCleared printer={{ ...printer, plate_clear: { ...printer.plate_clear, has_waiting_job: waiting, releases_job: kind === 'named' ? { id: 803, name: 'Lid' } : null } }} />)
  const modal = open()
  expect(modal.getByText(`Mark the plate on Workshop printer as cleared? ${waiting ? `${kind === 'hidden' ? 'A job' : 'Lid'} is waiting for it and will be released to start.` : 'Nothing will start.'}`)).toBeInTheDocument()
  expect(modal.getByRole('button', { name: waiting ? 'Plate cleared, release job' : 'Plate cleared' })).toBeInTheDocument()
  expect(state.press).not.toHaveBeenCalled()
})
it('prevents double presses and refreshes the confirmed server state', async () => {
  let resolve!: (data: any) => void
  state.press.mockImplementation(() => new Promise(done => { resolve = done }))
  setup(<Routes><Route path="/printers/:id" element={<PrinterDetail />} /></Routes>)
  await screen.findByRole('button', { name: 'Plate cleared' })
  const confirm = open().getByRole('button', { name: 'Plate cleared' })
  fireEvent.click(confirm); fireEvent.click(confirm)
  expect(state.press).toHaveBeenCalledExactlyOnceWith(199, { expected_release_job_id: null }); expect(confirm).toBeDisabled()
  state.get.mockResolvedValue({ ...printer, plate_clear: { ...printer.plate_clear, needed: false }, plate_clear_confirmed_at: '2020-01-01T13:00:00Z' })
  await act(async () => resolve({ success: true, message: 'Plate clear recorded' }))
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Plate cleared' })).not.toBeInTheDocument())
  expect(screen.getByText('Plate clear recorded')).toBeInTheDocument()
  expect(document.querySelector('time')).toHaveAttribute('dateTime', '2020-01-01T13:00:00Z')
})
it.each(['error', 'reply'])('shows the refusal verbatim without retry (%s)', async kind => {
  const message = 'Waiting job changed; current state preserved'
  if (kind === 'error') state.press.mockRejectedValue(new Error(message))
  else state.press.mockResolvedValue({ success: true, message })
  setup(); fireEvent.click(open().getByRole('button', { name: 'Plate cleared' }))
  expect(await screen.findByRole('status')).toHaveTextContent(message)
  expect(state.press).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button', { name: 'Plate cleared' })).toBeEnabled()
})
it('rechecks permission at confirmation', () => {
  setup(); const modal = open(); state.allowed = false
  fireEvent.click(modal.getByRole('button', { name: 'Plate cleared' })); expect(state.press).not.toHaveBeenCalled()
})
it.each([['classic', 390], ['foundry', 390], ['classic', 1280], ['foundry', 1280]] as const)('renders card and detail once in %s at width %s', async (family, width) => {
  document.documentElement.dataset.themeFamily = family
  vi.stubGlobal('matchMedia', vi.fn(query => ({ matches: width < 640 && query.includes('max-width'), addEventListener: vi.fn(), removeEventListener: vi.fn() })))
  const card = setup(family === 'classic' ? <ClassicCard printer={printer} {...props} /> : <FoundryCard printer={printer} />)
  expect(screen.getAllByRole('button', { name: 'Plate cleared' })).toHaveLength(1); open(); card.unmount()
  setup(<Routes><Route path="/printers/:id" element={<PrinterDetail />} /></Routes>)
  await screen.findByRole('button', { name: 'Plate cleared' })
  expect(screen.getAllByRole('button', { name: 'Plate cleared' })).toHaveLength(1)
  expect(screen.getByText('Separate start next')).toBeInTheDocument()
})

it.each(['bambu', 'moonraker', 'prusalink', 'elegoo', 'anycubic_lan'])('offers plate clearing for %s including Experimental printers', api_type => {
  setup(<ClassicCard printer={{ ...printer, api_type }} {...props} />)
  expect(screen.getByRole('button', { name: 'Plate cleared' })).toBeInTheDocument()
})

it('places the Classic plate row below the header occupied by the selection checkbox', () => {
  setup(<ClassicCard printer={printer} {...props} />)
  const card = screen.getByTestId('printer-card')
  const header = card.firstElementChild!
  expect(within(header as HTMLElement).getByRole('heading', { name: printer.name })).toBeInTheDocument()
  const plateRow = screen.getByText('Plate not cleared yet').parentElement!
  expect(header.nextElementSibling).toBe(plateRow)
  expect(plateRow).toContainElement(screen.getByRole('button', { name: 'Plate cleared' }))
})

it.each([true, false])('shows compact Foundry plate state and respects permission %s', allowed => {
  document.documentElement.dataset.themeFamily = 'foundry'
  state.allowed = allowed
  setup(<PrinterRows printers={[printer]} />)
  const row = screen.getByRole('row', { name: /Workshop printer/ })
  expect(within(row).getByText('Plate not cleared yet')).toBeInTheDocument()
  expect(!!within(row).queryByRole('button', { name: 'Plate cleared' })).toBe(allowed)
})

it('confirms and submits a compact Foundry plate clear through the shared control', async () => {
  document.documentElement.dataset.themeFamily = 'foundry'
  setup(<PrinterRows printers={[{ ...printer, plate_clear: { ...printer.plate_clear,
    has_waiting_job: true, releases_job: { id: 803, name: 'Lid' } } }]} />)
  const modal = open()
  expect(modal.getByText('Mark the plate on Workshop printer as cleared? Lid is waiting for it and will be released to start.')).toBeInTheDocument()
  expect(state.press).not.toHaveBeenCalled()
  fireEvent.click(modal.getByRole('button', { name: 'Plate cleared, release job' }))
  await waitFor(() => expect(state.press).toHaveBeenCalledExactlyOnceWith(199, { expected_release_job_id: 803 }))
})

it('shows the confirmed time without an action in a compact Foundry row', () => {
  document.documentElement.dataset.themeFamily = 'foundry'
  setup(<PrinterRows printers={[{ ...printer, plate_clear: { ...printer.plate_clear, needed: false },
    plate_clear_confirmed_at: '2020-01-01T13:00:00Z' }]} />)
  expect(screen.queryByRole('button', { name: 'Plate cleared' })).not.toBeInTheDocument()
  expect(document.querySelector('time')).toHaveAttribute('dateTime', '2020-01-01T13:00:00Z')
})

it.each(['none', 'named', 'hidden'])('sends the confirmation precondition for %s', async kind => {
  const plate = { ...printer.plate_clear, has_waiting_job: kind !== 'none', releases_job: kind === 'named' ? { id: 803, name: 'Lid' } : null }
  setup(<PlateCleared printer={{ ...printer, plate_clear: plate }} />)
  fireEvent.click(open().getByRole('button', { name: kind === 'none' ? 'Plate cleared' : 'Plate cleared, release job' }))
  await waitFor(() => expect(state.press).toHaveBeenCalledExactlyOnceWith(199,
    kind === 'hidden' ? { expected_has_waiting_job: true } : { expected_release_job_id: kind === 'named' ? 803 : null }))
})

it('keeps the confirmation snapshot when polling changes the printer', async () => {
  const client = new QueryClient()
  const view = (value: any) => <QueryClientProvider client={client}><PlateCleared printer={value} /></QueryClientProvider>
  const { rerender } = render(view(printer))
  open()
  rerender(view({ ...printer, plate_clear: { ...printer.plate_clear, has_waiting_job: true, releases_job: { id: 804, name: 'New lid' } } }))
  const modal = within(screen.getByRole('dialog'))
  expect(modal.getByText(/Nothing will start/)).toBeInTheDocument()
  fireEvent.click(modal.getByRole('button', { name: 'Plate cleared' }))
  await waitFor(() => expect(state.press).toHaveBeenCalledExactlyOnceWith(199, { expected_release_job_id: null }))
})

it.each(['named', 'hidden', 'none'])('reopens a changed queue confirmation for %s without retrying', async kind => {
  const message = 'The queue changed since you looked. Check what is waiting and confirm again.'
  const plate = { ...printer.plate_clear, has_waiting_job: kind !== 'none', releases_job: kind === 'named' ? { id: 804, name: 'New lid' } : null }
  state.press.mockRejectedValueOnce(Object.assign(new Error(message), { status: 409, detail: { message, plate_clear: plate } }))
  setup()
  fireEvent.click(open().getByRole('button', { name: 'Plate cleared' }))
  await waitFor(() => expect(within(screen.getByRole('dialog')).getByRole('status')).toHaveTextContent(message))
  const modal = within(screen.getByRole('dialog'))
  expect(modal.getByText(kind === 'none' ? /Nothing will start/ : kind === 'named' ? /New lid is waiting/ : /A job is waiting/)).toBeInTheDocument()
  expect(state.press).toHaveBeenCalledTimes(1)
  fireEvent.click(modal.getByRole('button', { name: kind === 'none' ? 'Plate cleared' : 'Plate cleared, release job' }))
  await waitFor(() => expect(state.press).toHaveBeenLastCalledWith(199,
    kind === 'hidden' ? { expected_has_waiting_job: true } : { expected_release_job_id: kind === 'named' ? 804 : null }))
  expect(state.press).toHaveBeenCalledTimes(2)
})
