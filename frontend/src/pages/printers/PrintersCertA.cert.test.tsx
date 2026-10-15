// Group A UI certification against the producer wire fixture captured by
// tests/test_contracts/test_cert_A_ui_wire_capture.py (real monitors + real routers).
import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import wire from '../../test-fixtures/cert-A-printers-wire.json'
import Printers from './Printers'
import useWebSocket from '../../hooks/useWebSocket'
import { PrinterInfoPanel } from '../../components/printers/PrinterPanels'
import PrinterModal from '../../components/printers/PrinterModal'

const state = vi.hoisted(() => ({
  user: { id: 6, username: 'operator', role: 'operator', group_id: 1 } as any,
  api: { telemetry: vi.fn(), hms: vi.fn(), nozzle: vi.fn(), nozzleHistory: vi.fn(), ams: vi.fn(), clearErrors: vi.fn(), fetchAPI: vi.fn() },
}))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, atPrinterLimit: () => false }) }))
vi.mock('../../permissions', () => ({ canDo: () => true, getCurrentUser: () => state.user }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 1 }) }))
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: any) => <div>{children}</div>,
  LineChart: ({ data, children }: any) => <div><div data-testid="chart-values">{JSON.stringify(data)}</div>{children}</div>,
  Line: ({ name }: any) => <span>{name}</span>, XAxis: () => null, YAxis: () => null, CartesianGrid: () => null, Tooltip: () => null, Legend: () => null,
}))
vi.mock('../../api', async original => {
  const real = await original<any>()
  return {
    ...real,
    fetchAPI: (...args: any[]) => state.api.fetchAPI(...args),
    getAmsEnvironment: (...args: any[]) => state.api.ams(...args),
    printers: { ...real.printers, clearErrors: (...args: any[]) => state.api.clearErrors(...args) },
    printerTelemetry: {
      ...real.printerTelemetry,
      get: (...args: any[]) => state.api.telemetry(...args),
      hmsHistory: (...args: any[]) => state.api.hms(...args),
      nozzle: (...args: any[]) => state.api.nozzle(...args),
      nozzleHistory: (...args: any[]) => state.api.nozzleHistory(...args),
    },
  }
})

const NOW = new Date('2026-10-14T12:00:10Z')
const w: any = wire
let socket: any

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  state.user = { id: 6, username: 'operator', role: 'operator', group_id: 1 }
  state.api.telemetry.mockResolvedValue(w.telemetry_301)
  state.api.hms.mockResolvedValue(w.hms_301)
  state.api.nozzle.mockResolvedValue(w.nozzle_301)
  state.api.nozzleHistory.mockResolvedValue(w.nozzle_history_301)
  state.api.ams.mockResolvedValue(w.ams_environment_301)
  state.api.clearErrors.mockResolvedValue(undefined)
  state.api.fetchAPI.mockResolvedValue(w.printers)
  class Socket { onmessage: any; constructor() { socket = this } close() {} send() {} }
  vi.stubGlobal('WebSocket', Socket)
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ token: 'synthetic-local-token' }) })))
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.clearAllMocks(); sessionStorage.clear(); localStorage.clear() })

async function page() {
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
  client.setQueryData(['printers', 1], w.printers)
  function Page() { useWebSocket(); return <Printers /> }
  const view = render(<QueryClientProvider client={client}><MemoryRouter><Page /></MemoryRouter></QueryClientProvider>)
  await waitFor(() => expect(socket?.onmessage).toBeTypeOf('function'))
  act(() => {
    socket.onmessage({ data: JSON.stringify({ type: 'printer_telemetry', data: w.ws_301 }) })
    for (const payload of Object.values(w.ws_other)) socket.onmessage({ data: JSON.stringify({ type: 'printer_telemetry', data: payload }) })
  })
  await waitFor(() => expect(within(card(301)).getByText('Cert Bambu')).toBeInTheDocument())
  await new Promise(resolve => setTimeout(resolve, 0))
  return view
}
const card = (id: number) => document.querySelector(`[data-printer-id="${id}"]`) as HTMLElement
const names = () => screen.queryAllByTestId('printer-card').map(c => c.getAttribute('data-printer-name'))

it('Bambu card shows live progress, ETA, speed slider and reported percentage during an active print', async () => {
  await page()
  const bambu = within(card(301))
  expect(await bambu.findByText(`${w.ws_301.progress}%`)).toBeInTheDocument()
  expect(bambu.getByText(`${w.ws_301.remaining_min}m left`)).toBeInTheDocument()
  expect(bambu.getByRole('progressbar', { name: 'Print progress' })).toHaveAttribute('aria-valuenow', String(w.ws_301.progress))
  expect(bambu.getByText(`Last reported speed: ${w.ws_301.speed_percent}%`)).toBeInTheDocument()
  expect(bambu.getByRole('slider', { name: 'Print speed mode' })).toHaveAttribute('max', '4')
})

// Moonraker/PrusaLink/Elegoo monitors persist PRINTING/PAUSED; utils/shared printActivity normalizes them with Bambu RUNNING/PAUSE.
it.each([401, 402, 403])('non-Bambu printing card %s shows progress (current wire)', async id => {
  await page()
  const payload = w.ws_other[String(id)]
  expect(within(card(id)).getByRole('progressbar', { name: 'Print progress' })).toHaveAttribute('aria-valuenow', String(payload.progress))
})

it('printing status filter keeps the actively printing Bambu printer', async () => {
  await page()
  fireEvent.change(screen.getByLabelText('Filter printers by status'), { target: { value: 'printing' } })
  expect(names()).toContain('Cert Bambu')
})
it('printing status filter includes Moonraker/PrusaLink/Elegoo printers that are printing', async () => {
  await page()
  fireEvent.change(screen.getByLabelText('Filter printers by status'), { target: { value: 'printing' } })
  expect(names().sort()).toEqual(['Cert Bambu', 'Cert Centauri', 'Cert MK4', 'Cert Voron'])
})

it('search, type/tag filters, sort and S/M/L/XL card size work on the real printer list', async () => {
  const view = await page()
  fireEvent.change(screen.getByPlaceholderText('Search printers...'), { target: { value: 'workshop' } })
  expect(names()).toEqual(['Cert Voron'])
  fireEvent.change(screen.getByPlaceholderText('Search printers...'), { target: { value: '' } })
  fireEvent.change(screen.getByLabelText('Filter printers by type'), { target: { value: 'prusalink' } })
  expect(names()).toEqual(['Cert MK4'])
  fireEvent.change(screen.getByLabelText('Filter printers by type'), { target: { value: 'all' } })
  fireEvent.change(screen.getByLabelText('Filter printers by tag'), { target: { value: 'farm-a' } })
  expect(names()).toEqual(['Cert Bambu'])
  fireEvent.change(screen.getByLabelText('Filter printers by tag'), { target: { value: '' } })
  fireEvent.change(screen.getByLabelText('Sort printers'), { target: { value: 'name_asc' } })
  expect(names()).toEqual(['Cert Bambu', 'Cert Centauri', 'Cert MK4', 'Cert Voron'])
  for (const size of ['S', 'M', 'L', 'XL']) expect(screen.getByRole('button', { name: size })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'L' }))
  expect(localStorage.getItem('printerCardSize')).toBe('L')
  view.unmount()
  await page()
  expect(screen.getByRole('button', { name: 'L' }).className).toContain('bg-[var(--brand-primary)]')
})

it('tags render on the card and every card has the AMS / Telemetry / Nozzle / HMS toolbar row', async () => {
  await page()
  expect(within(card(301)).getByText('farm-a')).toBeInTheDocument()
  for (const id of [301, 401, 402, 403]) {
    for (const name of ['AMS environment data', 'Print telemetry', 'Nozzle lifecycle', 'HMS error history']) {
      expect(within(card(id)).getByRole('button', { name })).toBeInTheDocument()
    }
  }
})

it('telemetry panel renders inline in its card with the stored 60s snapshots', async () => {
  await page()
  fireEvent.click(within(card(301)).getByRole('button', { name: 'Print telemetry' }))
  expect(await within(card(301)).findByRole('heading', { name: 'Print Telemetry' })).toBeInTheDocument()
  const values = JSON.parse(within(card(301)).getByTestId('chart-values').textContent!)
  expect(values).toHaveLength(w.telemetry_301.length)
  expect(values[1].nozzle).toBe(Number(w.telemetry_301[1].nozzle_temp.toFixed(1)))
  expect(state.api.telemetry).toHaveBeenCalledWith(301, 24)
  expect(within(card(401)).queryByRole('heading', { name: 'Print Telemetry' })).toBeNull()
})

it('HMS panel lists translated captured H2D errors and Clear Errors sends the reset', async () => {
  await page()
  fireEvent.click(within(card(301)).getByRole('button', { name: 'HMS error history' }))
  const panel = within(card(301))
  expect(await panel.findByText(`Top Recurring (${w.hms_301.total} total)`)).toBeInTheDocument()
  expect(panel.getAllByText(w.hms_301.entries[0].message).length).toBeGreaterThan(0)
  fireEvent.click(panel.getByRole('button', { name: 'Clear Errors' }))
  await waitFor(() => expect(state.api.clearErrors).toHaveBeenCalledWith(301))
})

it('nozzle panel shows the installed nozzle, install time and accumulated hours', async () => {
  await page()
  fireEvent.click(within(card(301)).getByRole('button', { name: 'Nozzle lifecycle' }))
  const panel = within(card(301))
  expect(await panel.findByRole('heading', { name: 'Nozzle Lifecycle' })).toBeInTheDocument()
  expect(await panel.findAllByText('0.4mm')).not.toHaveLength(0)
  expect(panel.getAllByText(/12\.5/).length).toBeGreaterThan(0)
})

it('AMS environment panel charts the captured humidity level and temperature per unit', async () => {
  await page()
  fireEvent.click(within(card(301)).getByRole('button', { name: 'AMS environment data' }))
  const panel = within(card(301))
  expect(await panel.findByRole('combobox', { name: 'AMS unit' })).toBeInTheDocument()
  expect(panel.getByText('Humidity level (1–5)')).toBeInTheDocument()
  const values = JSON.parse(panel.getByTestId('chart-values').textContent!)
  expect(values[0].temp).toBe(w.ams_environment_301.units['0'][0].temperature)
})

it('printer status panel shows live nozzle and bed temperatures from the wire', () => {
  const p = w.printers[0]
  render(<PrinterInfoPanel printer={p} />)
  expect(screen.getByText(`${Math.round(p.nozzle_temp)}°/${Math.round(p.nozzle_target_temp)}°C`)).toBeInTheDocument()
  expect(screen.getByText(`${Math.round(p.bed_temp)}°/${Math.round(p.bed_target_temp)}°C`)).toBeInTheDocument()
})
// Stored fan_speed is protocol-native: Bambu cooling_fan_speed gear 0-15 (captured A1: 11), PrusaLink fan RPM (5964).
it('fan speed is displayed per protocol unit: percent for Bambu/Moonraker/Elegoo, RPM for PrusaLink', () => {
  render(<div>{w.printers.map((p: any) => <PrinterInfoPanel key={p.id} printer={p} />)}</div>)
  const shown = screen.getAllByText(/^\d+%$/).map(e => Number(e.textContent!.replace('%', '')))
  expect(shown.every(v => v >= 0 && v <= 100)).toBe(true)
  expect(screen.getByText('73%')).toBeInTheDocument() // Bambu gear 11/15
  const prusa = w.printers.find((p: any) => p.api_type === 'prusalink')
  expect(screen.getByText(`${Math.round(prusa.fan_speed)} RPM`)).toBeInTheDocument()
})

it('emergency stop button lists every online active printer and stops one printer after confirmation', async () => {
  const { default: EmergencyStop } = await import('../../components/printers/EmergencyStop')
  state.api.fetchAPI.mockImplementation(async (path: string) => path === '/printers' ? w.printers : { success: true })
  render(<EmergencyStop />)
  const button = await screen.findByRole('button', { name: /Emergency controls, \d+ active prints/ })
  expect(button.className).toContain('text-red-400')
  fireEvent.click(button)
  const panel = within(screen.getByRole('region', { name: 'Emergency printer controls' }))
  await waitFor(() => expect(panel.getAllByRole('button', { name: 'Stop' })).toHaveLength(4))
  expect(panel.queryByRole('button', { name: /stop all/i })).toBeNull()
  fireEvent.click(panel.getAllByRole('button', { name: 'Stop' })[0])
  fireEvent.click(panel.getByRole('button', { name: 'Confirm Stop' }))
  await waitFor(() => expect(state.api.fetchAPI).toHaveBeenCalledWith('/printers/301/stop', { method: 'POST' }))
  expect(state.api.fetchAPI.mock.calls.filter(([path]) => String(path).endsWith('/stop'))).toHaveLength(1)
})

it('emergency stop is hidden from viewers', async () => {
  const { default: EmergencyStop } = await import('../../components/printers/EmergencyStop')
  state.user = { id: 1, username: 'student', role: 'viewer', group_id: 1 }
  render(<EmergencyStop />)
  expect(screen.queryByRole('button', { name: /Emergency controls/ })).toBeNull()
})

it('printer settings panel shows smart plug, camera URL, tags and timelapse fields and submits camera/tags/timelapse', () => {
  const onSubmit = vi.fn()
  render(<PrinterModal isOpen onClose={() => {}} onSubmit={onSubmit} printer={{ ...w.printers[0], camera_url: null }} />)
  expect(screen.getByRole('option', { name: 'No smart plug' })).toBeInTheDocument()
  fireEvent.submit(screen.getByRole('button', { name: /save|update/i }).closest('form')!)
  const [data, id] = onSubmit.mock.calls[0]
  expect(id).toBe(301)
  expect(data).toMatchObject({ tags: ['farm-a'], timelapse_enabled: false, camera_url: null })
})
// Plug settings travel as `plug` and Printers.tsx saves them through PUT /printers/{id}/plug.
it('smart plug configured in the printer settings panel is submitted for saving', () => {
  const onSubmit = vi.fn()
  render(<PrinterModal isOpen onClose={() => {}} onSubmit={onSubmit} printer={{ ...w.printers[0], camera_url: null }} />)
  const select = screen.getByRole('option', { name: 'No smart plug' }).closest('select')!
  fireEvent.change(select, { target: { value: 'tasmota' } })
  fireEvent.change(screen.getByPlaceholderText('Tasmota IP (e.g., 192.168.1.50)'), { target: { value: '192.0.2.60' } })
  fireEvent.submit(select.closest('form')!)
  const submitted = JSON.stringify(onSubmit.mock.calls[0][0])
  expect(submitted).toContain('tasmota')
  expect(submitted).toContain('192.0.2.60')
})

it('smart plug saved from the printer settings panel goes through PUT /printers/{id}/plug, not the printer PATCH', async () => {
  await page()
  const fetchMock = fetch as any
  fireEvent.click(within(card(401)).getByRole('button', { name: 'Edit printer settings' }))
  const select = (await screen.findByRole('option', { name: 'No smart plug' })).closest('select')!
  fireEvent.change(select, { target: { value: 'tasmota' } })
  fireEvent.change(screen.getByPlaceholderText('Tasmota IP (e.g., 192.168.1.50)'), { target: { value: '192.0.2.60' } })
  fireEvent.submit(select.closest('form')!)
  const call = (method: string, suffix: string) => fetchMock.mock.calls.find(([url, init]: any[]) =>
    String(url).endsWith(suffix) && (init?.method || 'GET') === method)
  await waitFor(() => expect(call('PUT', '/printers/401/plug')).toBeTruthy())
  expect(JSON.parse(call('PUT', '/printers/401/plug')[1].body)).toEqual({ type: 'tasmota', host: '192.0.2.60', entity_id: null })
  const patch = call('PATCH', '/printers/401')
  expect(patch).toBeTruthy()
  expect(JSON.parse(patch[1].body)).not.toHaveProperty('plug')
})

it('no printer-card control reaches the AMS RFID re-read endpoint (claim says it is triggered from the UI)', async () => {
  const { default: PrinterCard } = await import('../../components/printers/PrinterCard')
  const urls: string[] = []
  Object.defineProperty(navigator, 'clipboard', { value: { writeText: vi.fn(async () => {}) }, configurable: true })
  vi.stubGlobal('fetch', vi.fn(async (url: string) => { urls.push(String(url)); return { ok: true, status: 200, json: async () => ({}) } }))
  const props = { allFilaments: [], spools: [], onDelete: vi.fn(), onToggleActive: vi.fn(), onUpdateSlot: vi.fn(), onEdit: vi.fn(), onSyncAms: vi.fn() }
  render(<PrinterCard printer={{ ...w.printers[0], ...{ gcode_state: 'RUNNING', mc_percent: 34 } }} {...props} />)
  for (let pass = 0; pass < 2; pass++) {
    for (const button of screen.getAllByRole('button')) { await act(async () => { fireEvent.click(button) }) }
  }
  expect(state.api.fetchAPI.mock.calls.map(([p]) => String(p)).concat(urls).some(u => u.includes('/ams/refresh'))).toBe(false)
  expect(props.onSyncAms).toHaveBeenCalled() // the card's AMS action is the inventory sync, not an RFID re-read
})
