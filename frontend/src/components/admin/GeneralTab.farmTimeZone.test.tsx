// Farm time zone in Settings > General: with none saved the browser's zone is offered, unsaved,
// and nothing is stored until the admin presses the button; a quiet-hours window saved without a
// zone is offered the farm zone in its one-click button.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import GeneralTab from './GeneralTab'
const api = vi.hoisted(() => ({ update: vi.fn(async (d: unknown) => d), config: {} as Record<string, unknown> }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ tier: 'pro', isPro: true, max_printers: null, max_users: null, hasFeature: () => false }) }))
vi.mock('../../api', async importOriginal => ({ ...await importOriginal(), pricingConfig: { get: async () => ({}) },
  config: { get: async () => api.config, update: api.update, getRemoteAccess: vi.fn().mockResolvedValue({configured:false,url:null,check_url:null}), getNetwork: async () => ({}), testSpoolman: vi.fn() } }))
const realResolved = Intl.DateTimeFormat.prototype.resolvedOptions
beforeEach(() => {
  vi.spyOn(Intl.DateTimeFormat.prototype, 'resolvedOptions').mockImplementation(function (this: Intl.DateTimeFormat) {
    return { ...realResolved.call(this), timeZone: 'America/Chicago' }
  })
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.restoreAllMocks() })
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['stats'], { printers: { total: 1 } })
  return render(<QueryClientProvider client={client}><GeneralTab /></QueryClientProvider>)
}
const quiet = { spoolman_url: '', blackout_start: '00:00', blackout_end: '00:00', blackout_timezone: null }

it('with no farm zone saved, offers the browser zone unsaved and saves only on the button', async () => {
  api.config = { ...quiet, farm_timezone: null, farm_timezone_effective: { zone: 'UTC', source: 'utc' } }
  mount()
  const field = await screen.findByLabelText("Farm time zone: used for 'today', daily totals and quiet hours")
  await waitFor(() => expect(field).toHaveValue('America/Chicago'))
  expect(screen.getByTestId('farm-timezone-status')).toHaveTextContent("Not set. America/Chicago is your browser's time zone and is not saved yet. Until you save, \"today\" follows UTC (UTC, because nothing else is set).")
  expect(api.update).not.toHaveBeenCalled()
  // the page's other Save button never sends it
  fireEvent.click(screen.getByRole('button', { name: /Save Scheduling Settings/ }))
  await waitFor(() => expect(api.update).toHaveBeenCalledTimes(1))
  expect(api.update.mock.calls[0][0]).not.toHaveProperty('farm_timezone')
  fireEvent.click(screen.getByRole('button', { name: 'Save to use this for the whole farm' }))
  await waitFor(() => expect(api.update).toHaveBeenLastCalledWith({ farm_timezone: 'America/Chicago' }))
})

it('says which rule "today" follows until a zone is saved', async () => {
  api.config = { ...quiet, farm_timezone: null, farm_timezone_effective: { zone: 'America/New_York', source: 'server' } }
  mount()
  await waitFor(() => expect(screen.getByTestId('farm-timezone-status')).toHaveTextContent("\"today\" follows America/New_York (the server's TZ setting)."))
})

it('shows the saved farm zone; a change is saved only on the button', async () => {
  api.config = { ...quiet, farm_timezone: 'America/New_York', farm_timezone_effective: { zone: 'America/New_York', source: 'setting' } }
  mount()
  const field = await screen.findByLabelText(/^Farm time zone: used for/)
  await waitFor(() => expect(field).toHaveValue('America/New_York'))
  expect(screen.getByTestId('farm-timezone-status')).toHaveTextContent('"Today" and daily totals start at midnight, America/New_York time.')
  expect(screen.queryByRole('button', { name: 'Save to use this for the whole farm' })).toBeNull()
  expect(screen.getByRole('button', { name: 'Save farm time zone' })).toBeDisabled()
  fireEvent.change(field, { target: { value: 'UTC' } })
  expect(screen.getByTestId('farm-timezone-status')).toHaveTextContent('Not saved yet.')
  expect(api.update).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Save farm time zone' }))
  await waitFor(() => expect(api.update).toHaveBeenCalledWith({ farm_timezone: 'UTC' }))
})

it('a quiet-hours window with no zone is offered the farm zone, applied only on Save', async () => {
  api.config = { spoolman_url: '', blackout_start: '21:00', blackout_end: '06:00', blackout_timezone: null,
    farm_timezone: 'America/New_York', farm_timezone_effective: { zone: 'America/New_York', source: 'setting' } }
  mount()
  await waitFor(() => expect(screen.getByLabelText('Quiet hours start (UTC)')).toHaveValue('21:00'))
  expect(screen.getByTestId('quiet-hours-legacy-notice')).toHaveTextContent('applied in UTC')
  expect(screen.queryByRole('button', { name: /Use my time zone/ })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Use the farm time zone, America/New_York (keeps the same clock hours)' }))
  expect(screen.getByLabelText('Quiet hours time zone')).toHaveValue('America/New_York')
  expect(screen.getByLabelText('Quiet hours start (America/New_York)')).toHaveValue('21:00')
  expect(api.update).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: /Save Scheduling Settings/ }))
  await waitFor(() => expect(api.update).toHaveBeenCalledWith({ spoolman_url: '', blackout_start: '21:00', blackout_end: '06:00', blackout_timezone: 'America/New_York' }))
})

it('without a farm zone the quiet-hours button still offers the browser zone', async () => {
  api.config = { spoolman_url: '', blackout_start: '21:00', blackout_end: '06:00', blackout_timezone: null, farm_timezone: null, farm_timezone_effective: { zone: 'UTC', source: 'utc' } }
  mount()
  await waitFor(() => expect(screen.getByLabelText('Quiet hours start (UTC)')).toHaveValue('21:00'))
  expect(screen.getByRole('button', { name: 'Use my time zone (keeps the same clock hours)' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Use the farm time zone/ })).toBeNull()
})
