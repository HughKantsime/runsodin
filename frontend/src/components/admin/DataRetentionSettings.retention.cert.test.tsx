// Cert F / cluster D — F12-12.5-03 DataRetentionSettings component (also UI half of F12-12.5-01/-02).
// Real component + real api client (src/api retention -> fetchAPI). Only window.fetch is replaced, and it
// answers with producer wire JSON captured from the real backend by
// tests/test_contracts/test_cert_F_D_retention.py (artifacts/application-certification/cert-F/D-retention-wire.json).
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import DataRetentionSettings from './DataRetentionSettings'

const WIRE = {
  get_defaults: { alert_history_days: 90, audit_logs_days: 365, completed_jobs_days: 0, timelapses_days: 30 },
  put_result: { alert_history_days: 10, audit_logs_days: 60, completed_jobs_days: 30, timelapses_days: 7 },
  cleanup_result: {
    deleted: { alerts: 1, audit_logs: 1, classroom_oauth_states: 0, completed_jobs: 3 },
    refused: { timelapses: { reason: 'lifetime_retirement_pending', status: 'refused' } },
    status: 'partial',
  },
}

let calls: { url: string; method: string; body: unknown }[] = []
let policy: Record<string, number> = { ...WIRE.get_defaults }

function json(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

beforeEach(() => {
  calls = []
  policy = { ...WIRE.get_defaults }
  vi.stubGlobal('fetch', vi.fn((url: string, init: RequestInit = {}) => {
    const method = (init.method || 'GET').toUpperCase()
    const body = init.body ? JSON.parse(String(init.body)) : undefined
    calls.push({ url, method, body })
    if (url === '/api/config/retention' && method === 'GET') return json(policy)
    if (url === '/api/config/retention' && method === 'PUT') { policy = { ...policy, ...(body as object) }; return json(policy) }
    if (url === '/api/admin/retention/cleanup' && method === 'POST') return json(WIRE.cleanup_result)
    return json({ detail: 'unexpected' }, 500)
  }))
  vi.stubGlobal('confirm', vi.fn(() => true))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

const LABELS = ['Completed jobs', 'Audit logs', 'Timelapses', 'Alert history']

it('F12-12.5-03 loads the server policy into four labelled TTL inputs', async () => {
  render(<DataRetentionSettings />)
  for (const [label, value] of [['Completed jobs', '0'], ['Audit logs', '365'], ['Timelapses', '30'], ['Alert history', '90']]) {
    expect(await screen.findByLabelText(label)).toHaveValue(Number(value))
  }
  expect(calls[0]).toMatchObject({ url: '/api/config/retention', method: 'GET' })
})

it('F12-12.5-01 UI saves a per-type TTL as integers via PUT /api/config/retention', async () => {
  render(<DataRetentionSettings />)
  const values: Record<string, string> = { 'Completed jobs': '30', 'Audit logs': '60', Timelapses: '7', 'Alert history': '10' }
  for (const label of LABELS) fireEvent.change(await screen.findByLabelText(label), { target: { value: values[label] } })
  expect(screen.getByRole('button', { name: /Run Cleanup Now/ })).toBeDisabled()       // dirty draft blocks cleanup
  fireEvent.click(screen.getByRole('button', { name: /Save Policy/ }))
  expect(await screen.findByText('Retention policy saved')).toBeInTheDocument()
  const put = calls.find(c => c.method === 'PUT')!
  expect(put.body).toEqual(WIRE.put_result)
  expect(screen.getByRole('button', { name: /Run Cleanup Now/ })).not.toBeDisabled()
})

it('F12-12.5-03 rejects a negative / non-integer draft without calling the API', async () => {
  render(<DataRetentionSettings />)
  fireEvent.change(await screen.findByLabelText('Audit logs'), { target: { value: '-5' } })
  fireEvent.click(screen.getByRole('button', { name: /Save Policy/ }))
  expect(await screen.findByRole('alert')).toHaveTextContent(/nonnegative whole number/)
  expect(calls.filter(c => c.method === 'PUT')).toHaveLength(0)
})

it('F12-12.5-02 UI manual cleanup trigger confirms, POSTs and reports per-type deletion counts', async () => {
  render(<DataRetentionSettings />)
  await screen.findByLabelText('Audit logs')
  fireEvent.click(screen.getByRole('button', { name: /Run Cleanup Now/ }))
  expect(await screen.findByText('completed_jobs: 3 records deleted')).toBeInTheDocument()
  expect(screen.getByText('audit_logs: 1 records deleted')).toBeInTheDocument()
  expect(screen.getByText('alerts: 1 records deleted')).toBeInTheDocument()
  expect(vi.mocked(confirm)).toHaveBeenCalledTimes(1)
  expect(calls.filter(c => c.url === '/api/admin/retention/cleanup' && c.method === 'POST')).toHaveLength(1)
})

it('F12-12.5-02 UI cancelled confirmation never calls the cleanup route', async () => {
  vi.stubGlobal('confirm', vi.fn(() => false))
  render(<DataRetentionSettings />)
  await screen.findByLabelText('Audit logs')
  fireEvent.click(screen.getByRole('button', { name: /Run Cleanup Now/ }))
  await waitFor(() => expect(vi.mocked(confirm)).toHaveBeenCalled())
  expect(calls.filter(c => c.method === 'POST')).toHaveLength(0)
})

// Regression for F12-12.5-03 (fixed): the real backend answers {status:"partial", refused:{timelapses:...}}
// (timelapse TTL not applied); the component used to render "Cleanup complete:" and drop the refusal.
it('F12-12.5-03 partial cleanup (timelapses refused) is surfaced to the admin', async () => {
  render(<DataRetentionSettings />)
  await screen.findByLabelText('Timelapses')
  fireEvent.click(screen.getByRole('button', { name: /Run Cleanup Now/ }))
  await screen.findByText('completed_jobs: 3 records deleted')
  expect(screen.queryByText(/Cleanup complete/)).toBeNull()
  // Anchored on the result line: the "Timelapses" input label also matches /timelapses/i.
  expect(screen.getByText(/^timelapses:/i)).toHaveTextContent(/refused|not (applied|cleaned|deleted)/i)
})

// Regression for F12-12.5-03 (fixed): the description used to promise "automatic cleanup", but no backend
// scheduler applies the policy (tests/test_contracts/test_cert_F_D_retention.py::test_no_automatic_cleanup_runs_without_manual_trigger).
it('F12-12.5-03 description does not promise automatic cleanup that never runs', async () => {
  render(<DataRetentionSettings />)
  await screen.findByLabelText('Audit logs')
  expect(screen.queryByText(/automatic cleanup/i)).toBeNull()
})
