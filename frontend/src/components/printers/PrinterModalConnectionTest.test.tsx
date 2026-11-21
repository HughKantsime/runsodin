import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import PrinterModal from './PrinterModal'

const api = vi.hoisted(() => ({ testConnection: vi.fn() }))
vi.mock('../../api', () => ({ printers: api, getPlugConfig: vi.fn().mockResolvedValue(null) }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

const HOST = '192.0.2.77'
const SERIAL = 'FIXTURE-SERIAL-77'
const CODE = 'fixture-code-77'
const saved = { id: 199, name: 'shelf-1', model: 'P1S', slot_count: 4, api_type: 'bambu', api_host: HOST, api_key: `${SERIAL}|${CODE}` }

function fillNewBambu(submit = vi.fn()) {
  render(<PrinterModal isOpen onClose={vi.fn()} onSubmit={submit} />)
  fireEvent.change(screen.getByRole('option', { name: /Bambu/ }).closest('select')!, { target: { value: 'bambu' } })
  fireEvent.change(screen.getByPlaceholderText('e.g., p1s-01, x1c-floor2'), { target: { value: 'shelf-1' } })
  fireEvent.change(screen.getByPlaceholderText('e.g., 192.168.1.100'), { target: { value: HOST } })
  fireEvent.change(screen.getByPlaceholderText('e.g., 00M09A380700000'), { target: { value: SERIAL } })
  fireEvent.change(screen.getByPlaceholderText('e.g., 12345678'), { target: { value: CODE } })
  return submit
}
const test = () => fireEvent.click(screen.getByRole('button', { name: 'Test Connection' }))
const failure = async () => { const message = await screen.findByTestId('connection-test-message'); await waitFor(() => expect(message.className).toMatch(/text-red-400/)); return message }

it.each([
  ['unreachable', /^ODIN could not reach a printer at that address\. Check that the printer is on/],
  ['auth', /^The printer answered but did not accept the credentials\. Re-enter the access code/],
  ['not_this_type', /^Something answered at that address, but not as the printer type you chose\. Check the printer type/],
  ['blocked_address', /^ODIN does not connect to that address\. Enter the printer's own address/],
  ['control_disabled', /^The printer answered, but it is not accepting control from ODIN\. Turn on LAN Only mode and Developer Mode/],
])('reason %s shows one sentence and one thing to do, without the address or credentials', async (reason, sentence) => {
  api.testConnection.mockResolvedValue({ success: false, error: 'Failed to connect. Check IP, serial, and access code.', reason })
  fillNewBambu()
  test()
  const message = await failure()
  expect(message).toHaveTextContent(sentence)
  expect(message.textContent).not.toContain(HOST)
  expect(message.textContent).not.toContain(SERIAL)
  expect(message.textContent).not.toContain(CODE)
  expect(message.textContent).not.toContain('Failed to connect')
})

it.each(['unknown', undefined])('reason %s keeps the server sentence and the checklist', async reason => {
  api.testConnection.mockResolvedValue({ success: false, error: 'Failed to connect. Check IP, serial, and access code.', reason })
  fillNewBambu()
  test()
  expect(await failure()).toHaveTextContent(/^Failed to connect\. Check IP, serial, and access code\. Check that ODIN and the printer are on the same network/)
})

it('asks "Save anyway?" for a new printer whose connection was never tested', () => {
  const submit = fillNewBambu()
  fireEvent.click(screen.getByRole('button', { name: 'Add Printer' }))
  expect(submit).not.toHaveBeenCalled()
  expect(screen.getByTestId('save-anyway-prompt')).toHaveTextContent('The connection has not been tested with these details. Save anyway?')
  fireEvent.click(screen.getByRole('button', { name: 'Go back' }))
  expect(screen.queryByTestId('save-anyway-prompt')).toBeNull()
  expect(submit).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Add Printer' }))
  fireEvent.click(screen.getByRole('button', { name: 'Save anyway' }))
  expect(submit).toHaveBeenCalledTimes(1)
  expect(submit.mock.calls[0][0]).toMatchObject({ api_type: 'bambu', api_host: HOST })
})

it('asks "Save anyway?" after a failed test, and says the test failed', async () => {
  api.testConnection.mockResolvedValue({ success: false, error: 'x', reason: 'auth' })
  const submit = fillNewBambu()
  test()
  await failure()
  fireEvent.click(screen.getByRole('button', { name: 'Add Printer' }))
  expect(submit).not.toHaveBeenCalled()
  expect(screen.getByTestId('save-anyway-prompt')).toHaveTextContent(/^The last connection test failed.*Save anyway\?$/)
  fireEvent.click(screen.getByRole('button', { name: 'Save anyway' }))
  expect(submit).toHaveBeenCalledTimes(1)
})

it('saves without asking after a passing test, and asks again once the tested details change', async () => {
  api.testConnection.mockResolvedValue({ success: true, state: 'idle', bed_temp: 0, ams_slots: 4 })
  const submit = fillNewBambu()
  test()
  await screen.findByText(/^Connected!/)
  fireEvent.change(screen.getByPlaceholderText('e.g., 192.168.1.100'), { target: { value: '192.0.2.78' } })
  fireEvent.click(screen.getByRole('button', { name: 'Add Printer' }))
  expect(submit).not.toHaveBeenCalled()
  expect(screen.getByTestId('save-anyway-prompt')).toBeInTheDocument()
  fireEvent.change(screen.getByPlaceholderText('e.g., 192.168.1.100'), { target: { value: HOST } })
  expect(screen.queryByTestId('save-anyway-prompt')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Add Printer' }))
  expect(submit).toHaveBeenCalledTimes(1)
})

it('editing a saved printer asks only when its connection details changed or its test failed', async () => {
  const submit = vi.fn()
  render(<PrinterModal isOpen onClose={vi.fn()} onSubmit={submit} printer={saved} />)
  fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
  expect(submit).toHaveBeenCalledTimes(1) // unchanged connection: no question
  fireEvent.change(screen.getByPlaceholderText('e.g., 192.168.1.100'), { target: { value: '192.0.2.78' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
  expect(submit).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('save-anyway-prompt')).toBeInTheDocument()
  fireEvent.change(screen.getByPlaceholderText('e.g., 192.168.1.100'), { target: { value: HOST } })
  api.testConnection.mockResolvedValue({ success: false, error: 'x', reason: 'unreachable' })
  test()
  await failure()
  fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
  expect(submit).toHaveBeenCalledTimes(1)
  expect(screen.getByTestId('save-anyway-prompt')).toHaveTextContent(/last connection test failed/)
})

it.each([4, 8, 0])('shows both tray counts and submits the standard count when the printer reports %i', async standard => {
  const submit = fillNewBambu()
  const slots = screen.getByRole('option', { name: '12 slots (3x AMS)' }).closest('select')!
  fireEvent.change(slots, { target: { value: '12' } })
  api.testConnection.mockResolvedValue({ success: true, state: 'idle', bed_temp: 0, ams_slots: standard, ams_ht_slots: 1 })
  test()
  expect(await screen.findByText(/^Connected!/)).toHaveTextContent(`${standard} AMS slots, 1 AMS HT`)
  fireEvent.click(screen.getByRole('button', { name: 'Add Printer' }))
  expect(submit.mock.calls[0][0].slot_count).toBe(standard || 12)
})

it('renames a printer with no stored bed and submits only the bed axis the owner later edits', () => {
  const submit = vi.fn()
  render(<PrinterModal isOpen onClose={vi.fn()} onSubmit={submit} printer={{ ...saved, model: 'H2D', bed_x_mm: null, bed_y_mm: null }} />)
  fireEvent.change(screen.getByPlaceholderText('e.g., p1s-01, x1c-floor2'), { target: { value: 'Renamed' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
  expect(submit.mock.calls[0][0].name).toBe('Renamed')
  expect(submit.mock.calls[0][0]).not.toHaveProperty('bed_x_mm')
  expect(submit.mock.calls[0][0]).not.toHaveProperty('bed_y_mm')
  fireEvent.change(screen.getByPlaceholderText('X mm'), { target: { value: '315' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
  expect(submit.mock.calls[1][0].bed_x_mm).toBe(315)
  expect(submit.mock.calls[1][0]).not.toHaveProperty('bed_y_mm')
})

it('retesting a saved printer leaves its slot count for Sync AMS to repair safely', async () => {
  const submit = vi.fn()
  render(<PrinterModal isOpen onClose={vi.fn()} onSubmit={submit} printer={{ ...saved, slot_count: 5 }} />)
  api.testConnection.mockResolvedValue({ success: true, state: 'idle', bed_temp: 0, ams_slots: 4, ams_ht_slots: 1 })
  test()
  expect(await screen.findByText(/^Connected!/)).toHaveTextContent('4 AMS slots, 1 AMS HT')
  fireEvent.click(screen.getByRole('button', { name: 'Save Changes' }))
  expect(submit.mock.calls[0][0]).not.toHaveProperty('slot_count')
})

it('creates a Prusa MK4 with its suggested 250 by 210 mm bed', async () => {
  const submit = vi.fn()
  api.testConnection.mockResolvedValue({ success: true })
  render(<PrinterModal isOpen onClose={vi.fn()} onSubmit={submit} />)
  fireEvent.change(screen.getByRole('option', { name: /PrusaLink/ }).closest('select')!, { target: { value: 'prusalink' } })
  fireEvent.change(screen.getByPlaceholderText('e.g., p1s-01, x1c-floor2'), { target: { value: 'workshop-mk4' } })
  fireEvent.change(screen.getByPlaceholderText('e.g., Prusa MK4, Ender 3'), { target: { value: 'Prusa MK4' } })
  fireEvent.change(screen.getByPlaceholderText('e.g., 192.168.1.100'), { target: { value: HOST } })
  test()
  await screen.findByText(/^Connected!/)
  fireEvent.click(screen.getByRole('button', { name: 'Add Printer' }))
  expect(submit.mock.calls[0][0]).toMatchObject({ api_type: 'prusalink', model: 'Prusa MK4', bed_x_mm: 250, bed_y_mm: 210 })
})

it.each([[5, 4], [9, 4], [8, 8]])('switching to Bambu with %i slots submits %i', (initial, expected) => {
  const submit = fillNewBambu()
  const type = screen.getByRole('option', { name: /PrusaLink/ }).closest('select')!
  fireEvent.change(type, { target: { value: 'prusalink' } })
  const slots = screen.getByRole('option', { name: '8 slots (2x AMS)' }).closest('select')!
  fireEvent.change(slots, { target: { value: String(initial) } })
  fireEvent.change(type, { target: { value: 'bambu' } })
  fireEvent.click(screen.getByRole('button', { name: 'Add Printer' }))
  fireEvent.click(screen.getByRole('button', { name: 'Save anyway' }))
  expect(submit.mock.calls[0][0].slot_count).toBe(expected)
  expect(slots).toHaveValue(String(expected))
})

it('offers H3C when adding a Bambu printer', () => {
  fillNewBambu()
  const select = screen.getByRole('option', { name: 'H3C' }).closest('select')!
  fireEvent.change(select, { target: { value: 'H3C' } })
  expect(select.value).toBe('H3C')
  expect(screen.getByRole('option', { name: '1 slot (no AMS)' }).closest('select')).toHaveValue('1')
})
