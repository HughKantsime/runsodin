import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import NotificationsTab from './NotificationsTab'

vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ id: 1, role: 'admin', group_id: null }) }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, hasFeature: () => true }) }))
vi.mock('../../hooks/usePushNotifications', () => ({
  default: () => ({ isSupported: false, isSubscribed: false, permission: 'default', loading: false }),
}))

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    let body: unknown
    if (url === '/api/alert-preferences') body = [
      { alert_type: 'PRINT_FAILED', in_app: true, browser_push: false, email: false },
      { alert_type: 'spool_low', in_app: false, browser_push: true, email: true, threshold_value: 100 },
    ]
    else if (url === '/api/config/pause-reminders') body = { enabled: true, first_hours: 2, repeat_hours: 12 }
    else if (url === '/api/smtp-config') body = { enabled: false }
    else if (url === '/api/config/temperature-alerts') body = {
      enabled: true, under_c: 15, over_c: 15, sustain_minutes: 2, warmup_minutes: 5,
      idle_hot_nozzle_c: 60, idle_hot_bed_c: 50, disabled_printer_ids: [],
    }
    else if (url.startsWith('/api/printers')) body = []
    else throw new Error(`Unexpected request: ${url}`)
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it.each(['Print Failed', 'Spool Low'])('names each channel for the displayed %s alert and links its requirement', async label => {
  render(<NotificationsTab />)
  const inApp = await screen.findByRole('switch', { name: `In-app alerts for ${label}` })
  const push = screen.getByRole('switch', { name: `Push alerts for ${label}` })
  const email = screen.getByRole('switch', { name: `Email alerts for ${label}` })
  expect(push).toHaveAccessibleDescription('Requires browser push setup')
  expect(email).toHaveAccessibleDescription('Requires SMTP configuration')
  for (const control of [push, email]) {
    expect(document.getElementById(control.getAttribute('aria-describedby')!)).toBeInTheDocument()
  }
  expect(screen.queryByRole('switch', { name: 'Requires browser push setup' })).not.toBeInTheDocument()
  expect(screen.queryByRole('switch', { name: 'Requires SMTP configuration' })).not.toBeInTheDocument()
  const checked = inApp.getAttribute('aria-checked')
  fireEvent.click(inApp)
  expect(inApp).toHaveAttribute('aria-checked', checked === 'true' ? 'false' : 'true')
})

it('names the SMTP master switch and retains its toggle behavior', async () => {
  render(<NotificationsTab />)
  const smtp = await screen.findByRole('switch', { name: 'Email notifications (SMTP)' })
  expect(smtp).toHaveAttribute('aria-checked', 'false')
  fireEvent.click(smtp)
  expect(smtp).toHaveAttribute('aria-checked', 'true')
})

it('gives every rendered switch an accessible name, including temperature alerts', async () => {
  render(<NotificationsTab />)
  await screen.findByRole('switch', { name: 'In-app alerts for Print Failed' })
  await screen.findByRole('switch', { name: 'Temperature anomaly alerts' })
  await waitFor(() => expect(screen.getAllByRole('switch')).toHaveLength(33))
  for (const control of screen.getAllByRole('switch')) expect(control).toHaveAccessibleName()
})
