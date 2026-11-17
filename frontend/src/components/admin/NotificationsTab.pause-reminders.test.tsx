import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import NotificationsTab from './NotificationsTab'

const actor = vi.hoisted(() => ({ role: 'admin', group_id: 1 }))
vi.mock('../../permissions', () => ({ getCurrentUser: () => actor }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: false, hasFeature: () => false }) }))
vi.mock('../../hooks/usePushNotifications', () => ({ default: () => ({ isSupported: false, isSubscribed: false, loading: false }) }))
let loadError = false
let saveError = false
let puts: unknown[]
const stored = { enabled: true, first_hours: 2, repeat_hours: 12 }
const json = (value: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } }))
beforeEach(() => {
  actor.role = 'admin'
  loadError = false
  saveError = false
  puts = []
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (url === '/api/alert-preferences') return json([])
    if (url === '/api/config/pause-reminders') {
      if (init?.method === 'PUT') {
        puts.push(JSON.parse(String(init.body)))
        return saveError ? json({ detail: 'repeat_hours must be 0 (no repeat) or between 1 and 168.' }, 422) : json(puts.at(-1))
      }
      return loadError ? json({ detail: 'Unavailable' }, 500) : json(stored)
    }
    throw new Error(`Unexpected request: ${url}`)
  }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('lets an admin edit and save the farm settings beneath Print Paused', async () => {
  render(<NotificationsTab />)
  const control = await screen.findByRole('switch', { name: 'Remind me when a print stays paused' })
  expect(control).toBeChecked()
  fireEvent.click(control)
  fireEvent.change(screen.getByRole('spinbutton', { name: 'After (hours)' }), { target: { value: '0.25' } })
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Then every (hours, 0 for once)' }), { target: { value: '0' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save pause reminders' }))
  await screen.findByText('Reminder settings saved')
  expect(puts).toEqual([{ enabled: false, first_hours: 0.25, repeat_hours: 0 }])
  expect(screen.getByText(/settings apply to the whole farm/)).toBeInTheDocument()
})

it.each(['operator', 'viewer'])('shows read-only settings to a %s', async role => {
  actor.role = role
  render(<NotificationsTab />)
  expect(await screen.findByRole('switch', { name: 'Remind me when a print stays paused' })).toBeDisabled()
  expect(screen.getByRole('spinbutton', { name: 'After (hours)' })).toHaveAttribute('readonly')
  expect(screen.getByRole('spinbutton', { name: 'Then every (hours, 0 for once)' })).toHaveAttribute('readonly')
  expect(screen.queryByRole('button', { name: 'Save pause reminders' })).not.toBeInTheDocument()
})

it('shows the server refusal sentence on save', async () => {
  saveError = true
  render(<NotificationsTab />)
  await screen.findByRole('switch', { name: 'Remind me when a print stays paused' })
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Then every (hours, 0 for once)' }), { target: { value: '0.5' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save pause reminders' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('repeat_hours must be 0 (no repeat) or between 1 and 168.')
})

it('leaves the rest of Notifications usable after a load error', async () => {
  loadError = true
  render(<NotificationsTab />)
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not load reminder settings')
  const other = screen.getByRole('switch', { name: 'In-app alerts for Print Failed' })
  fireEvent.click(other)
  await waitFor(() => expect(other).toHaveAttribute('aria-checked', 'false'))
})
