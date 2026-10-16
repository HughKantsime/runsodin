// cert-D jobsA F04-4.6-02: a viewer (student) submits a Model Library model for approval on the real
// Models page with the real permissions module and API client, replaying the producer wire captured by
// tests/test_contracts/test_cert_D_jobsA_approval_workflow.py::test_capture_certD_jobsA_approval_wire.
// Only window.fetch (the HTTP boundary) is substituted; unrecorded list endpoints answer [].
import React from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Models from './Models'
import { refreshPermissions, clearSensitiveBrowserState } from '../../permissions'
import wire from '../../test-fixtures/certD-jobsA-approval-wire.json'

vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: null }) }))
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }))

type Call = { method: string; url: string }
let calls: Call[] = []
let approval: unknown = wire.approval_setting

function reply(status: number, body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

beforeEach(() => {
  calls = []
  approval = wire.approval_setting
  vi.stubGlobal('fetch', vi.fn((input: string, init: RequestInit = {}) => {
    const method = (init.method || 'GET').toUpperCase()
    const url = String(input)
    calls.push({ method, url })
    if (url === '/api/auth/me') return reply(200, wire.me.student)
    if (url === '/api/permissions') return reply(200, wire.permissions)
    if (url === '/api/config/require-job-approval') return reply(200, approval)
    if (method === 'GET' && url.startsWith('/api/models-with-pricing')) return reply(200, [wire.library_model])
    if (method === 'GET' && url === '/api/models/78/schedule-options') return reply(wire.library_schedule_options.status, wire.library_schedule_options.body)
    if (method === wire.library_submit.method && url === '/api' + wire.library_submit.path) return reply(wire.library_submit.status, wire.library_submit.body)
    if (method === 'GET') return reply(200, [])
    return reply(500, { detail: 'unrecorded request ' + method + ' ' + url })
  }))
})
afterEach(async () => { cleanup(); await clearSensitiveBrowserState(); vi.unstubAllGlobals() })

async function show() {
  await refreshPermissions()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], wire.me.student)
  render(<QueryClientProvider client={client}><MemoryRouter><Models /></MemoryRouter></QueryClientProvider>)
  await screen.findByRole('heading', { name: 'Wire library model' })
}

it('F04-4.6-02 student submits a library model for approval and the producer creates a Submitted job', async () => {
  await show()
  const button = await screen.findByRole('button', { name: 'Submit for approval' })
  fireEvent.click(button)
  const dialog = await screen.findByRole('dialog')
  await waitFor(() => expect(calls.some(c => c.url === '/api/models/78/schedule-options')).toBe(true))
  const confirm = within(dialog).getByRole('button', { name: 'Create Job' })
  await waitFor(() => expect(confirm).not.toBeDisabled())
  fireEvent.click(confirm)
  await waitFor(() => expect(calls.filter(c => c.method === 'POST').map(c => c.url)).toEqual(['/api/models/78/schedule']))
  expect(wire.library_submit.body.status).toBe('submitted')
})

it('F04-4.6-02 without approval mode the student gets no library submit control', async () => {
  approval = { require_job_approval: false }
  await show()
  expect(screen.queryByRole('button', { name: 'Submit for approval' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Schedule print job' })).not.toBeInTheDocument()
})
