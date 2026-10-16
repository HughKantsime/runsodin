// cert-D jobsA: real Jobs page + real permissions module + real API client, replaying the producer
// wire captured by tests/test_contracts/test_cert_D_jobsA_approval_workflow.py::test_capture_certD_jobsA_approval_wire.
// Only window.fetch (the HTTP boundary) is substituted; unrecorded list endpoints answer [].
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Jobs from './Jobs'
import { refreshPermissions, clearSensitiveBrowserState } from '../../permissions'
import wire from '../../test-fixtures/certD-jobsA-approval-wire.json'

type Call = { method: string; url: string; body: unknown }
let calls: Call[] = []
let jobsQueue: unknown[][] = []
let principal: keyof typeof wire.me = 'operator1'

function reply(status: number, body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function installFetch() {
  vi.stubGlobal('fetch', vi.fn((input: string, init: RequestInit = {}) => {
    const method = (init.method || 'GET').toUpperCase()
    const url = String(input)
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined
    calls.push({ method, url, body })
    if (url === '/api/auth/me') return reply(200, wire.me[principal])
    if (url === '/api/permissions') return reply(200, wire.permissions)
    if (url === '/api/config/require-job-approval') return reply(200, wire.approval_setting)
    if (method === 'GET' && url === '/api/jobs') return reply(200, jobsQueue.length > 1 ? jobsQueue.shift() : jobsQueue[0])
    for (const step of [wire.approve, wire.reject, wire.resubmit, wire.correct]) {
      if (method === step.method && url === '/api' + step.path) return reply(step.status, step.body)
    }
    if (method === 'GET') return reply(200, [])
    return reply(500, { detail: 'unrecorded request ' + method + ' ' + url })
  }))
}

async function show(user: keyof typeof wire.me, jobsResponses: unknown[][]) {
  principal = user
  jobsQueue = jobsResponses
  await refreshPermissions()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], wire.me[user])
  render(<QueryClientProvider client={client}><MemoryRouter><Jobs /></MemoryRouter></QueryClientProvider>)
  await screen.findByText('Wire operator pending')
}

const row = (name: string) => screen.getByText(name, { exact: true }).closest('tr')!
const submittedCount = (rows: { status: string }[]) => rows.filter(j => j.status === 'submitted').length

beforeEach(() => { calls = []; installFetch() })
afterEach(async () => { cleanup(); await clearSensitiveBrowserState(); vi.unstubAllGlobals() })

describe('F04-4.6-03 Awaiting Approval tab with badge count', () => {
  it.each(['operator1', 'admin'] as const)('%s sees the badge count of real submitted rows and the tab filters to them', async user => {
    await show(user, [wire.jobs_operator])
    const tab = screen.getByRole('button', { name: /Awaiting Approval/ })
    const expected = submittedCount(wire.jobs_operator)
    expect(expected).toBeGreaterThan(0)
    expect(tab).toHaveTextContent(String(expected))
    fireEvent.click(tab)
    expect(screen.getByText('Wire student submitted')).toBeInTheDocument()
    expect(screen.queryByText('Wire operator pending')).not.toBeInTheDocument()
    expect(screen.queryByText('Wire student rejected')).not.toBeInTheDocument()
    expect(within(row('Wire student submitted')).getByRole('button', { name: 'Approve job' })).toBeInTheDocument()
  })
})

describe('F04-4.6-02 viewer submission shows Submitted status', () => {
  it('student sees own submission with the submitted badge and no approve controls', async () => {
    await show('student', [wire.jobs_viewer])
    expect(within(row('Wire student submitted')).getByText('submitted')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve job' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reject job' })).not.toBeInTheDocument()
  })
})

describe('F04-4.6-04 Approve/Reject buttons', () => {
  it('Approve posts to the recorded endpoint and the refreshed queue shows the job pending', async () => {
    await show('operator1', [wire.jobs_operator, wire.jobs_operator_after_approve])
    fireEvent.click(within(row('Wire student submitted')).getByRole('button', { name: 'Approve job' }))
    await waitFor(() => expect(calls.some(c => c.method === 'POST' && c.url === '/api' + wire.approve.path)).toBe(true))
    await waitFor(() => expect(within(row('Wire student submitted')).getByText('pending')).toBeInTheDocument())
    expect(within(row('Wire student submitted')).queryByRole('button', { name: 'Approve job' })).not.toBeInTheDocument()
  })

  it('Reject requires a reason and posts exactly the recorded request body', async () => {
    await show('operator1', [wire.jobs_operator_reject_view])
    fireEvent.click(within(row('Wire reject target')).getByRole('button', { name: 'Reject job' }))
    const dialog = await screen.findByRole('dialog')
    const submit = within(dialog).getByRole('button', { name: 'Reject' })
    expect(submit).toBeDisabled()
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: '  ' + wire.reject.request.reason + '  ' } })
    fireEvent.click(submit)
    await waitFor(() => expect(calls.find(c => c.method === 'POST' && c.url === '/api' + wire.reject.path)?.body).toEqual(wire.reject.request))
  })
})

describe('F04-4.1-03 / F04-4.6-05 rejected job: student edits and resubmits (generic Jobs page)', () => {
  // JobResponse now serializes submitted_by, rejected_reason and charged_to_org_id, which JobRow gates
  // Resubmit / viewer Edit on and shows as the rejection reason.
  it('original submitter sees Resubmit on own rejected job and it posts the recorded request', async () => {
    await show('student', [wire.jobs_viewer])
    fireEvent.click(within(row('Wire student rejected')).getByRole('button', { name: 'Resubmit job' }))
    await waitFor(() => expect(calls.some(c => c.method === 'POST' && c.url === '/api' + wire.resubmit.path)).toBe(true))
  })

  it('original submitter can open Edit on own rejected job and send the recorded correction', async () => {
    await show('student', [wire.jobs_viewer])
    fireEvent.click(within(row('Wire student rejected')).getByRole('button', { name: 'Edit job' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Notes' }), { target: { value: wire.correct.request.notes } })
    fireEvent.click(screen.getByRole('button', { name: 'Save correction' }))
    await waitFor(() => expect(calls.find(c => c.method === 'PATCH' && c.url === '/api' + wire.correct.path)?.body).toEqual(wire.correct.request))
  })

  it('rejected row shows the rejection reason to the submitter', async () => {
    await show('student', [wire.jobs_viewer])
    expect(within(row('Wire student rejected')).getByText('Wire reason: reduce infill')).toBeInTheDocument()
  })

  it('another user never gets Resubmit on a foreign rejected job', async () => {
    await show('student', [wire.jobs_viewer])
    expect(within(row('Wire outsider rejected')).queryByRole('button', { name: 'Resubmit job' })).not.toBeInTheDocument()
  })
})
