// Cert F / cluster B — F11-11.6-03 "APITokenManager component for CRUD" (and F11-11.6-01/02 UI half).
// Real component + real api/auth.ts adapter + real fetchAPI; only global fetch is stubbed, answering with
// wire fixtures captured from the real backend (tests/test_contracts/test_cert_F_B_api_tokens.py, LiveOdin).
// Token value in the capture is replaced by a same-shape placeholder (no real secret).
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import APITokenManager from './APITokenManager'

// WIRE-BEGIN
const WIRE: any = {
 "tokens_list": [
  {
   "id": 1,
   "name": "Cert prefix token",
   "prefix": "odin_XmlCX",
   "scopes": [
    "read:printers"
   ],
   "expires_at": null,
   "last_used_at": null,
   "created_at": "2026-10-15 00:57:10"
  }
 ],
 "tokens_create": {
  "id": 1,
  "name": "Cert prefix token",
  "prefix": "odin_XmlCX",
  "scopes": [
   "read:printers"
  ],
  "expires_at": null,
  "created_at": "2026-10-15T00:57:10.531777+00:00",
  "token": "odin_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
 }
}
// WIRE-END

const state = vi.hoisted(() => ({ tokens: [] as any[], fetch: vi.fn() }))
function respond(data: any, status = 200) { return { ok: status < 400, status, json: async () => data, text: async () => JSON.stringify(data), headers: new Headers() } }
function calls(path: string, method: string) { return state.fetch.mock.calls.filter(([u, o]) => String(u) === path && (o?.method || 'GET') === method) }
beforeEach(() => {
  state.tokens = structuredClone(WIRE.tokens_list)
  state.fetch.mockReset()
  state.fetch.mockImplementation(async (url: unknown, options: any = {}) => {
    const path = String(url); const method = options.method || 'GET'
    if (path === '/api/tokens' && method === 'GET') return respond(state.tokens)
    if (path === '/api/tokens' && method === 'POST') {
      const body = JSON.parse(options.body)
      const created = { ...WIRE.tokens_create, id: 2, name: body.name, scopes: body.scopes }
      state.tokens = [{ id: 2, name: body.name, prefix: created.prefix, scopes: body.scopes, expires_at: null, last_used_at: null, created_at: WIRE.tokens_list[0].created_at }, ...state.tokens]
      return respond(created)
    }
    const m = path.match(/^\/api\/tokens\/(\d+)$/)
    if (m && method === 'DELETE') { state.tokens = state.tokens.filter(t => t.id !== Number(m[1])); return respond({ status: 'ok' }) }
    return respond({ detail: 'unexpected ' + method + ' ' + path }, 500)
  })
  vi.stubGlobal('fetch', state.fetch)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('F11-11.6-01 lists the wire token with its odin_ prefix and scopes, never the secret', async () => {
  render(<APITokenManager />)
  expect(await screen.findByText(WIRE.tokens_list[0].name)).toBeInTheDocument()
  expect(screen.getByText(WIRE.tokens_list[0].prefix + '...')).toBeInTheDocument()
  expect(WIRE.tokens_list[0].prefix.startsWith('odin_')).toBe(true)
  expect(screen.getByText('read:printers')).toBeInTheDocument()
})

it('F11-11.6-02 offers the granular resource scopes read:printers and write:jobs', async () => {
  render(<APITokenManager />); fireEvent.click(await screen.findByText('New Token'))
  for (const label of ['Read Printers', 'Write Printers', 'Read Jobs', 'Write Jobs', 'Read Spools', 'Write Spools', 'Read Models', 'Write Models', 'Read Analytics', 'Full Admin']) {
    expect(screen.getByRole('button', { name: label })).toBeInTheDocument()
  }
})

it('F11-11.6-03 create: POSTs name/scopes/expiry, reveals the odin_ token once and refreshes the list', async () => {
  render(<APITokenManager />); fireEvent.click(await screen.findByText('New Token'))
  fireEvent.change(screen.getByLabelText('Token name'), { target: { value: 'CI pipeline' } })
  fireEvent.click(screen.getByRole('button', { name: 'Write Jobs' }))
  fireEvent.change(screen.getByLabelText(/Expires in/), { target: { value: '30' } })
  fireEvent.click(screen.getByText('Create Token'))
  await waitFor(() => expect(calls('/api/tokens', 'POST')).toHaveLength(1))
  expect(JSON.parse(calls('/api/tokens', 'POST')[0][1].body)).toEqual({ name: 'CI pipeline', scopes: ['write:jobs'], expires_days: 30 })
  expect(await screen.findByText(WIRE.tokens_create.token)).toBeInTheDocument()
  expect(await screen.findByText('CI pipeline')).toBeInTheDocument()
  expect(calls('/api/tokens', 'GET').length).toBeGreaterThanOrEqual(2)
  fireEvent.click(screen.getByText('Dismiss'))
  expect(screen.queryByText(WIRE.tokens_create.token)).not.toBeInTheDocument()
})

it('F11-11.6-03 revoke: DELETE /api/tokens/{id} removes the row', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  render(<APITokenManager />)
  fireEvent.click(await screen.findByRole('button', { name: 'Revoke token ' + WIRE.tokens_list[0].name }))
  await waitFor(() => expect(calls('/api/tokens/' + WIRE.tokens_list[0].id, 'DELETE')).toHaveLength(1))
  await waitFor(() => expect(screen.queryByText(WIRE.tokens_list[0].name)).not.toBeInTheDocument())
})

it('F11-11.6-03 (claim_mismatch evidence) a listed token offers no edit/update control, only revoke', async () => {
  render(<APITokenManager />)
  const row = (await screen.findByText(WIRE.tokens_list[0].name)).closest('div.flex.items-center.justify-between') as HTMLElement
  expect(within(row).getAllByRole('button').map(b => b.getAttribute('aria-label'))).toEqual(['Revoke token ' + WIRE.tokens_list[0].name])
})
