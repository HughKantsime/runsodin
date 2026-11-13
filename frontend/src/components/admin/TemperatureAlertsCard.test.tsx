// Settings > Notifications "Temperature anomaly alerts": loads GET /api/config/temperature-alerts, saves the whole
// object with PUT, turns whole feature and single printers off, shows a refusal, and stays away when it cannot load.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import TemperatureAlertsCard from './TemperatureAlertsCard'

const STORED = {
  enabled: true, under_c: 15, over_c: 15, sustain_minutes: 5, warmup_minutes: 8,
  idle_hot_nozzle_c: 60, idle_hot_bed_c: 45, disabled_printer_ids: [] as number[],
}
let getStatus = 200
let putStatus = 200
let puts: any[] = []

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

beforeEach(() => {
  getStatus = 200
  putStatus = 200
  puts = []
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit = {}) => {
    const method = (init.method || 'GET').toUpperCase()
    if (url === '/api/config/temperature-alerts' && method === 'GET') return json(getStatus === 200 ? STORED : { detail: 'Superadmin access required' }, getStatus)
    if (url === '/api/config/temperature-alerts' && method === 'PUT') {
      const body = JSON.parse(String(init.body))
      puts.push(body)
      return putStatus === 200 ? json(body) : json({ detail: 'under_c must be a number from 5 to 100' }, putStatus)
    }
    if (url === '/api/printers?active_only=true') return json([{ id: 1, name: 'Xena', nickname: null }, { id: 2, name: 'Yoda', nickname: 'Yo' }])
    return json({ detail: 'unexpected' }, 500)
  }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('shows the stored thresholds, the master switch and the printers', async () => {
  render(<TemperatureAlertsCard />)
  expect(await screen.findByRole('switch', { name: 'Temperature anomaly alerts' })).toHaveAttribute('aria-checked', 'true')
  expect(screen.getByLabelText('Not reaching target')).toHaveValue(15)
  expect(screen.getByLabelText('Warm-up grace')).toHaveValue(8)
  expect(await screen.findByRole('checkbox', { name: 'Temperature alerts for Xena' })).toBeChecked()
  expect(screen.getByRole('checkbox', { name: 'Temperature alerts for Yo' })).toBeChecked()
})

it('saves an edited threshold and a printer switched off', async () => {
  render(<TemperatureAlertsCard />)
  fireEvent.change(await screen.findByLabelText('Not reaching target'), { target: { value: '20' } })
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Temperature alerts for Xena' }))
  fireEvent.click(screen.getByRole('button', { name: 'Save temperature alerts' }))
  expect(await screen.findByText('Saved')).toBeInTheDocument()
  expect(puts).toEqual([{ ...STORED, under_c: 20, disabled_printer_ids: [1] }])
  expect(screen.getByRole('checkbox', { name: 'Temperature alerts for Xena' })).not.toBeChecked()
})

it('switches the whole feature off', async () => {
  render(<TemperatureAlertsCard />)
  fireEvent.click(await screen.findByRole('switch', { name: 'Temperature anomaly alerts' }))
  expect(screen.getByRole('switch', { name: 'Temperature anomaly alerts' })).toHaveAttribute('aria-checked', 'false')
  fireEvent.click(screen.getByRole('button', { name: 'Save temperature alerts' }))
  await waitFor(() => expect(puts).toEqual([{ ...STORED, enabled: false }]))
})

it('refuses an empty number without calling the server', async () => {
  render(<TemperatureAlertsCard />)
  fireEvent.change(await screen.findByLabelText('Overshoot'), { target: { value: '' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save temperature alerts' }))
  expect(await screen.findByText('Enter a number for "Overshoot".')).toBeInTheDocument()
  expect(puts).toEqual([])
})

it('shows the server refusal', async () => {
  putStatus = 422
  render(<TemperatureAlertsCard />)
  fireEvent.change(await screen.findByLabelText('Not reaching target'), { target: { value: '1' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save temperature alerts' }))
  expect(await screen.findByText('under_c must be a number from 5 to 100')).toBeInTheDocument()
})

it('renders nothing when the settings cannot be loaded (not a platform admin)', async () => {
  getStatus = 403
  const { container } = render(<TemperatureAlertsCard />)
  await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/config/temperature-alerts', expect.anything()))
  expect(container).toBeEmptyDOMElement()
})
