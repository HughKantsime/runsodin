/**
 * Cert F / cluster C - F11-11.8-01/02/04: QuotaManager wired to the real quota API contract.
 *
 * Real QuotaManager + real api client (quotas.adminList/adminSet -> fetchAPI). window.fetch is the only
 * boundary and replays producer wire captured from a real ODIN server (test_cert_F_C_ui_wire.py checks the
 * embedded JSON against that capture).
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import QuotaManager from './QuotaManager'

const WIRE = /*WIRE-BEGIN*/{"admin_quota_put": {"body": {"status": "ok"}, "status": 200}, "admin_quotas": [{"quota_grams": null, "quota_hours": null, "quota_jobs": null, "quota_period": "monthly", "usage": {"grams_used": 0, "hours_used": 0, "jobs_used": 0}, "user_id": 1, "username": "certadmin"}, {"quota_grams": 500.0, "quota_hours": 10.0, "quota_jobs": 5, "quota_period": "semester", "usage": {"grams_used": 0.0, "hours_used": 0.0, "jobs_used": 1}, "user_id": 2, "username": "ui_operator"}, {"quota_grams": null, "quota_hours": null, "quota_jobs": null, "quota_period": "monthly", "usage": {"grams_used": 0, "hours_used": 0, "jobs_used": 0}, "user_id": 3, "username": "ui_viewer"}]}/*WIRE-END*/

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function serve(put: { status: number, body: unknown } = WIRE.admin_quota_put) {
  const calls: Array<{ url: string, method: string, body: any }> = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method || 'GET'
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined })
    if (url === '/api/admin/quotas' && method === 'GET') return json(200, WIRE.admin_quotas)
    if (url.startsWith('/api/admin/quotas/') && method === 'PUT') return json(put.status, put.body)
    return json(404, { detail: 'Not Found' })
  }))
  return calls
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function row(username: string) {
  return screen.getByText(username).closest('tr') as HTMLElement
}

it('F11-11.8-01 renders every user with jobs, grams and hours limits and real usage', async () => {
  serve()
  render(<QuotaManager />)
  await screen.findByText('ui_operator')
  const r = row('ui_operator')
  const inputs = within(r).getAllByRole('spinbutton') as HTMLInputElement[]
  expect(inputs.map(i => i.value)).toEqual(['5', '500', '10'])
  expect(within(r).getByText('1/5 jobs (20%)')).toBeInTheDocument()
  expect(within(r).getByText('0/500g')).toBeInTheDocument()
  expect(within(r).getByText('0.0/10h')).toBeInTheDocument()
  expect(within(row('ui_viewer')).getByText('No limit')).toBeInTheDocument()
  for (const h of ['Jobs', 'Grams', 'Hours', 'Period', 'Usage']) expect(screen.getByRole('columnheader', { name: h })).toBeInTheDocument()
})

it('F11-11.8-02 period selector offers exactly daily, weekly, monthly, semester and shows the stored period', async () => {
  serve()
  render(<QuotaManager />)
  const select = await screen.findByRole('combobox', { name: 'Quota period for ui_operator' }) as HTMLSelectElement
  expect(Array.from(select.options).map(o => o.value)).toEqual(['daily', 'weekly', 'monthly', 'semester'])
  expect(select.value).toBe('semester')
})

it('F11-11.8-04 editing limits and period sends the real PUT contract and clears a limit with null', async () => {
  const calls = serve()
  render(<QuotaManager />)
  await screen.findByText('ui_operator')
  const r = row('ui_operator')
  const [jobs, grams] = within(r).getAllByRole('spinbutton')
  fireEvent.change(jobs, { target: { value: '9' } })
  fireEvent.change(grams, { target: { value: '' } })
  fireEvent.change(within(r).getByRole('combobox'), { target: { value: 'daily' } })
  fireEvent.click(within(r).getByRole('button'))
  await waitFor(() => expect(calls.some(c => c.method === 'PUT')).toBe(true))
  const put = calls.find(c => c.method === 'PUT')!
  expect(put.url).toBe('/api/admin/quotas/2')
  expect(put.body).toEqual({ quota_jobs: 9, quota_grams: null, quota_period: 'daily' })
  // after the 200 {status: ok} the row keeps the saved values and the save button disappears
  await waitFor(() => expect(within(r).queryByRole('button')).not.toBeInTheDocument())
  expect((within(r).getAllByRole('spinbutton')[0] as HTMLInputElement).value).toBe('9')
  expect((within(r).getByRole('combobox') as HTMLSelectElement).value).toBe('daily')
})

it('F11-11.8-04 a refused save shows the server detail and keeps the unsaved edit', async () => {
  serve({ status: 403, body: { detail: 'Cannot set quotas for users outside your group' } })
  render(<QuotaManager />)
  await screen.findByText('ui_operator')
  const r = row('ui_operator')
  fireEvent.change(within(r).getAllByRole('spinbutton')[0], { target: { value: '3' } })
  fireEvent.click(within(r).getByRole('button'))
  expect(await screen.findByText('Cannot set quotas for users outside your group')).toBeInTheDocument()
  expect((within(r).getAllByRole('spinbutton')[0] as HTMLInputElement).value).toBe('3')
  expect(within(r).getByRole('button')).toBeInTheDocument()
})
