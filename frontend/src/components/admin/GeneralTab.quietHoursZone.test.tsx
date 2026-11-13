// Quiet hours time zone in Settings > General: the saved zone is shown and editable; a window saved
// without one is said to be UTC and offers one explicit button; nothing is changed without a click.
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
const save = () => fireEvent.click(screen.getByRole('button', { name: /Save Scheduling Settings/ }))

it('shows the saved time zone, in the labels and the summary, and saves it back', async () => {
  api.config = { spoolman_url: '', blackout_start: '22:30', blackout_end: '05:30', blackout_timezone: 'America/New_York' }
  mount()
  await waitFor(() => expect(screen.getByLabelText('Quiet hours time zone')).toHaveValue('America/New_York'))
  expect(screen.getByLabelText('Quiet hours start (America/New_York)')).toHaveValue('22:30')
  expect(screen.getByLabelText('Quiet hours end (America/New_York)')).toHaveValue('05:30')
  expect(screen.getByTestId('quiet-hours-summary')).toHaveTextContent('between 22:30 and 05:30, America/New_York time, all year')
  expect(screen.queryByTestId('quiet-hours-legacy-notice')).toBeNull()
  expect(screen.queryByRole('button', { name: /Use my time zone/ })).toBeNull()
  save()
  await waitFor(() => expect(api.update).toHaveBeenCalledWith({ spoolman_url: '', blackout_start: '22:30', blackout_end: '05:30', blackout_timezone: 'America/New_York' }))
})

it('the time zone can be changed', async () => {
  api.config = { spoolman_url: '', blackout_start: '22:30', blackout_end: '05:30', blackout_timezone: 'America/New_York' }
  mount()
  await waitFor(() => expect(screen.getByLabelText('Quiet hours time zone')).toHaveValue('America/New_York'))
  fireEvent.change(screen.getByLabelText('Quiet hours time zone'), { target: { value: 'UTC' } })
  expect(screen.getByTestId('quiet-hours-summary')).toHaveTextContent('between 22:30 and 05:30, UTC time')
  save()
  await waitFor(() => expect(api.update).toHaveBeenCalledWith(expect.objectContaining({ blackout_timezone: 'UTC' })))
})

it('a window saved without a time zone says it is UTC and is saved unchanged unless the button is pressed', async () => {
  api.config = { spoolman_url: '', blackout_start: '21:00', blackout_end: '06:00', blackout_timezone: null }
  mount()
  await waitFor(() => expect(screen.getByLabelText('Quiet hours start (UTC)')).toHaveValue('21:00'))
  expect(screen.getByTestId('quiet-hours-legacy-notice')).toHaveTextContent('saved without a time zone, so they are applied in UTC')
  expect(screen.getByLabelText('Quiet hours time zone')).toHaveValue('')
  expect(screen.getByTestId('quiet-hours-summary')).toHaveTextContent('between 21:00 and 06:00 UTC')
  save()
  await waitFor(() => expect(api.update).toHaveBeenCalledWith({ spoolman_url: '', blackout_start: '21:00', blackout_end: '06:00' }))
})

it('"Use my time zone" keeps the clock hours, names the browser zone, and applies only on Save', async () => {
  api.config = { spoolman_url: '', blackout_start: '21:00', blackout_end: '06:00' }
  mount()
  await waitFor(() => expect(screen.getByLabelText('Quiet hours start (UTC)')).toHaveValue('21:00'))
  fireEvent.click(screen.getByRole('button', { name: 'Use my time zone (keeps the same clock hours)' }))
  expect(screen.getByLabelText('Quiet hours time zone')).toHaveValue('America/Chicago')
  expect(screen.getByLabelText('Quiet hours start (America/Chicago)')).toHaveValue('21:00')
  expect(screen.getByLabelText('Quiet hours end (America/Chicago)')).toHaveValue('06:00')
  expect(screen.getByTestId('quiet-hours-summary')).toHaveTextContent('between 21:00 and 06:00, America/Chicago time')
  expect(screen.queryByTestId('quiet-hours-legacy-notice')).toBeNull()
  expect(api.update).not.toHaveBeenCalled()
  save()
  await waitFor(() => expect(api.update).toHaveBeenCalledWith({ spoolman_url: '', blackout_start: '21:00', blackout_end: '06:00', blackout_timezone: 'America/Chicago' }))
})

it('quiet hours that are off show no notice; a window set now is saved in the browser time zone', async () => {
  api.config = { spoolman_url: '', blackout_start: '00:00', blackout_end: '00:00', blackout_timezone: null }
  mount()
  await waitFor(() => expect(screen.getByLabelText('Quiet hours time zone')).toHaveValue('America/Chicago'))
  expect(screen.getByTestId('quiet-hours-summary')).toHaveTextContent(/^Quiet hours are off/)
  expect(screen.queryByTestId('quiet-hours-legacy-notice')).toBeNull()
  fireEvent.change(screen.getByLabelText('Quiet hours start (America/Chicago)'), { target: { value: '22:00' } })
  fireEvent.change(screen.getByLabelText('Quiet hours end (America/Chicago)'), { target: { value: '06:00' } })
  save()
  await waitFor(() => expect(api.update).toHaveBeenCalledWith({ spoolman_url: '', blackout_start: '22:00', blackout_end: '06:00', blackout_timezone: 'America/Chicago' }))
})
