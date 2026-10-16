// Cert-E (rep): F10-10.8-01..04 UI evidence.
// Real EducationReports + real ProGate + real LicenseProvider + real api client.
// Only window.fetch is stubbed; it replays producer fixtures captured from the
// real routers in tests/test_contracts/test_cert_E_rep_usage.py:
//   cert-E-rep-usage-wire.json   (GET /education/usage-report?days=7|30|90, real signed Pro license)
//   cert-E-rep-license-wire.json (GET /license for real signed Community and Pro licenses)
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import EducationReports from './EducationReports'
import ProGate from '../../components/shared/ProGate'
import { LicenseProvider } from '../../LicenseContext'
import usageWire from '../../test-fixtures/cert-E-rep-usage-wire.json'
import licenseWire from '../../test-fixtures/cert-E-rep-license-wire.json'

vi.mock('recharts', async original => {
  const actual = await original<typeof import('recharts')>()
  return { ...actual, ResponsiveContainer: ({ children }: any) => React.cloneElement(children, { width: 800, height: 300 }) }
})
vi.mock('../../permissions', () => ({ clearSensitiveBrowserState: vi.fn() }))

let tier: 'pro' | 'community' = 'pro'
let requested: string[] = []
function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}
beforeEach(() => {
  tier = 'pro'; requested = []
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo) => {
    const url = String(input)
    requested.push(url)
    if (url === '/api/license') return json(200, licenseWire.bodies[tier])
    if (url.startsWith('/api/education/usage-report')) {
      const days = new URLSearchParams(url.split('?')[1]).get('days') as string
      // Only captured windows are replayed; other windows are not invented.
      const body = (usageWire.windows as Record<string, unknown>)[days]
      return body ? json(200, body) : json(503, { detail: 'not captured' })
    }
    return json(200, {})
  }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.clearAllMocks() })

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter><LicenseProvider>
    <ProGate feature="usage_reports" tier="Pro"><EducationReports /></ProGate>
  </LicenseProvider></MemoryRouter></QueryClientProvider>)
}

async function show90() {
  mount()
  await screen.findByRole('heading', { name: 'Usage Reports' })
  fireEvent.click(screen.getByRole('button', { name: '90d' }))
  await screen.findByRole('heading', { name: 'Usage by User' })
  await screen.findByText('Cert bracket')
}

test('CertERep F10-10.8-02 window presets are 7/14/30/90 days and drive the days query', async () => {
  mount()
  await screen.findByRole('heading', { name: 'Usage Reports' })
  expect(['7d', '14d', '30d', '90d'].map(n => screen.getByRole('button', { name: n }))).toHaveLength(4)
  await waitFor(() => expect(requested).toContain('/api/education/usage-report?days=30'))
  const table = () => screen.getByRole('region', { name: 'Usage by user table' })
  const submitted = async (user: string, n: string) => waitFor(() =>
    expect(within(within(table()).getByText(user).closest('tr')!).getAllByRole('cell')[1].textContent).toBe(n))
  await submitted('student', '2')
  fireEvent.click(screen.getByRole('button', { name: '7d' }))
  await waitFor(() => expect(requested).toContain('/api/education/usage-report?days=7'))
  await submitted('student', '1')
  fireEvent.click(screen.getByRole('button', { name: '90d' }))
  await waitFor(() => expect(requested).toContain('/api/education/usage-report?days=90'))
  await submitted('student', '3')
  expect(requested.filter(u => u.includes('usage-report')).every(u => /days=(7|14|30|90)$/.test(u))).toBe(true)
})

test('CertERep F10-10.8-01 per-user jobs, hours, cost and accuracy render from captured report', async () => {
  await show90()
  const table = screen.getByRole('region', { name: 'Usage by user table' })
  const student = within(table).getByText('student').closest('tr')!
  const cells = within(student).getAllByRole('cell').map(c => c.textContent)
  expect(cells[1]).toBe('3')          // submitted
  expect(cells[5]).toBe('9h')         // actual hours
  expect(cells[9]).toBe('88.9%')      // time accuracy
  expect(cells[10]).toBe('Est. EUR 18.00')
  const manager = within(table).getByText('manager').closest('tr')!
  const m = within(manager).getAllByRole('cell').map(c => c.textContent)
  expect([m[1], m[5], m[9], m[10]]).toEqual(['2', '1h', '100%', 'Est. EUR 5.00'])
})

test('CertERep F10-10.8-03 by-printer and by-model aggregates render from captured report', async () => {
  await show90()
  const printers = screen.getByRole('region', { name: 'Usage by printer table' })
  const rows = (name: string, region: HTMLElement) => within(within(region).getByText(name).closest('tr')!).getAllByRole('cell').map(c => c.textContent)
  expect(rows('P1S-09', printers)).toEqual(['P1S-09', '4', '7h', '100%', 'Est. EUR 20.00'])
  expect(rows('P1S-10', printers)).toEqual(['P1S-10', '1', '3h', '66.7%', 'Est. EUR 3.00'])
  const models = screen.getByRole('region', { name: 'Usage by model table' })
  expect(rows('Cert bracket', models)).toEqual(['Cert bracket', '3', '9h', '88.9%', 'Est. EUR 18.00'])
  expect(rows('Cert hinge', models)).toEqual(['Cert hinge', '2', '1h', '100%', 'Est. EUR 5.00'])
})

test('CertERep F10-10.8-04 Community signed license is gated at the Pro tier and never queries the report', async () => {
  tier = 'community'
  mount()
  expect(await screen.findByRole('heading', { name: 'Pro Feature' })).toBeInTheDocument()
  expect(screen.queryByRole('heading', { name: 'Usage Reports' })).not.toBeInTheDocument()
  expect(requested.some(u => u.includes('usage-report'))).toBe(false)
})
