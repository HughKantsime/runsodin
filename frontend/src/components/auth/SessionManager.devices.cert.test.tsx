// Cert F / cluster B — F11-11.7-02/03 Active sessions UI. Real component + real api/auth.ts adapter; only global
// fetch is stubbed with the GET /api/sessions body captured from the real backend
// (tests/test_contracts/test_cert_F_B_sessions.py, LiveOdin, desktop Chrome + iPhone Safari logins).
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import SessionManager from './SessionManager'
import ErrorBoundary from '../shared/ErrorBoundary'

// WIRE-BEGIN
const WIRE: any = {
 "sessions_list": [
  {
   "id": 3,
   "ip_address": "10.77.0.1",
   "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
   "created_at": "2026-10-15 00:59:40",
   "last_seen_at": "2026-10-15 00:59:41.927322+00:00",
   "is_current": true
  },
  {
   "id": 4,
   "ip_address": "10.77.0.2",
   "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
   "created_at": "2026-10-15 00:59:41",
   "last_seen_at": "2026-10-15 00:59:41",
   "is_current": false
  },
  {
   "id": 5,
   "ip_address": "10.77.0.3",
   "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
   "created_at": "2026-10-15 00:59:41",
   "last_seen_at": "2026-10-15 00:59:41",
   "is_current": false
  }
 ]
}
// WIRE-END

const state = vi.hoisted(() => ({ sessions: [] as any[], fetch: vi.fn() }))
function respond(data: any, status = 200) { return { ok: status < 400, status, json: async () => data, text: async () => JSON.stringify(data), headers: new Headers() } }
function calls(path: string, method: string) { return state.fetch.mock.calls.filter(([u, o]) => String(u) === path && (o?.method || 'GET') === method) }
beforeEach(() => {
  state.sessions = structuredClone(WIRE.sessions_list)
  state.fetch.mockReset()
  state.fetch.mockImplementation(async (url: unknown, options: any = {}) => {
    const path = String(url); const method = options.method || 'GET'
    if (path === '/api/sessions' && method === 'GET') return respond(state.sessions)
    if (path === '/api/sessions' && method === 'DELETE') { const n = state.sessions.filter(s => !s.is_current).length; state.sessions = state.sessions.filter(s => s.is_current); return respond({ status: 'ok', revoked: n }) }
    const m = path.match(/^\/api\/sessions\/(\d+)$/)
    if (m && method === 'DELETE') { state.sessions = state.sessions.filter(s => s.id !== Number(m[1])); return respond({ status: 'ok' }) }
    return respond({ detail: 'unexpected' }, 500)
  })
  vi.stubGlobal('fetch', state.fetch)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

it.each([
  {admin: false, expired: '/api/sessions'},
  {admin: true, expired: '/api/sessions'},
  {admin: true, expired: '/api/admin/sessions'},
])('renders nothing during the login redirect from $expired (admin=$admin)', async ({admin, expired}) => {
  state.fetch.mockImplementation(async url => String(url) === expired ? respond({}, 401) : respond([]))
  const {container} = render(<ErrorBoundary><SessionManager admin={admin} /></ErrorBoundary>)
  await waitFor(() => expect(container).toBeEmptyDOMElement())
  expect(calls(expired, 'GET')).toHaveLength(1)
})

it('F11-11.7-02 lists each active session with device, browser, IP and the current-device badge', async () => {
  render(<SessionManager />)
  expect(await screen.findByText('Active Sessions')).toBeInTheDocument()
  expect(screen.getAllByText('Chrome on Desktop')).toHaveLength(2)
  expect(screen.getByText('Safari on Mobile')).toBeInTheDocument()
  for (const s of WIRE.sessions_list) expect(screen.getByText(new RegExp(s.ip_address.replace(/\./g, '\\.')))).toBeInTheDocument()
  expect(screen.getAllByText('This device')).toHaveLength(1)
})

it('F11-11.7-03 revokes one other session through DELETE /api/sessions/{id}', async () => {
  render(<SessionManager />)
  const phone = WIRE.sessions_list.find((s: any) => /iPhone/.test(s.user_agent))
  const row = (await screen.findByText('Safari on Mobile')).closest('div.flex.items-center.justify-between') as HTMLElement
  fireEvent.click(row.querySelector('button')!)
  await waitFor(() => expect(calls('/api/sessions/' + phone.id, 'DELETE')).toHaveLength(1))
  await waitFor(() => expect(screen.queryByText('Safari on Mobile')).not.toBeInTheDocument())
  expect(screen.getAllByText('Chrome on Desktop')).toHaveLength(2)
})

it('F11-11.7-03 signs out everywhere else through DELETE /api/sessions and keeps only this device', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<SessionManager />)
  fireEvent.click(await screen.findByRole('button', { name: /Sign out everywhere else/ }))
  await waitFor(() => expect(calls('/api/sessions', 'DELETE')).toHaveLength(1))
  await waitFor(() => expect(screen.getAllByText(/on (Desktop|Mobile)/)).toHaveLength(1))
  expect(screen.getByText('This device')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /Sign out everywhere else/ })).not.toBeInTheDocument()
})

it('lists and ends an administrator-visible session through the admin endpoints', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  state.fetch.mockResolvedValueOnce(respond([{...WIRE.sessions_list[1], username: 'Morgan', user_id: 2}]))
    .mockResolvedValueOnce(respond(state.sessions))
    .mockResolvedValueOnce(respond({detail: 'Session not found'}, 404))
    .mockResolvedValueOnce(respond({status: 'ok'}))
  render(<SessionManager admin />)
  await screen.findByText('Morgan')
  expect(screen.getByText('Safari on Mobile')).toBeVisible()
  expect(screen.getByText(/10\.77\.0\.2.*Last seen/)).toBeVisible()
  const end = screen.getByRole('button', {name: 'End session for Morgan'})
  fireEvent.click(end)
  expect(await screen.findByRole('alert')).toHaveTextContent('Session not found')
  fireEvent.click(end)
  expect(await screen.findByText('No active sessions.')).toBeVisible()
  expect(calls('/api/admin/sessions', 'GET')).toHaveLength(1)
  expect(calls('/api/admin/sessions/4', 'DELETE')).toHaveLength(2)
  expect(calls('/api/sessions', 'GET')).toHaveLength(1)
  expect(calls('/api/sessions/4', 'DELETE')).toHaveLength(0)
})

it('identifies the current admin session by id and signs out only after confirmed revocation', async () => {
  const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValue(true)
  const location = {pathname: '/settings', href: '/settings'}
  vi.stubGlobal('location', location)
  const {is_current, ...row} = WIRE.sessions_list[0]
  state.fetch.mockResolvedValueOnce(respond([{...row, username: 'Admin'}, {...row, id: 17, username: 'Other'}]))
    .mockResolvedValueOnce(respond(state.sessions))
    .mockResolvedValueOnce(respond({detail: 'Session not found'}, 404))
    .mockResolvedValueOnce(respond({status: 'ok'}))
  render(<SessionManager admin />)
  const end = await screen.findByRole('button', {name: 'End session for Admin'})
  expect(screen.getAllByText('This device')).toHaveLength(1)
  expect(end.parentElement).toHaveTextContent('This device')
  fireEvent.click(end)
  expect(confirm).toHaveBeenCalledWith(expect.stringMatching(/signs? you out/i))
  expect(calls('/api/admin/sessions/3', 'DELETE')).toHaveLength(0)
  fireEvent.click(end)
  await screen.findByRole('alert')
  expect(location.href).toBe('/settings')
  fireEvent.click(end)
  await waitFor(() => expect(location.href).toBe('/login'))
})
