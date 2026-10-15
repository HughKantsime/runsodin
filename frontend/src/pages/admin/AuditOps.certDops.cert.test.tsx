// cert-D ops: F19-19.1-01/02 audit log viewer against the producer wire captured by
// tests/test_contracts/test_cert_D_ops_bulk_audit.py (actual GET /permissions defaults
// and an actual authenticated audited bulk mutation). Real permissions module,
// RoleGate (same page key as App.tsx) and auditLogs client run; transport is stubbed.
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import AuditLogs from './AuditLogs'
import RoleGate from '../../components/auth/RoleGate'
import { refreshPermissions } from '../../permissions'
import wire from '../../test-fixtures/certDops-audit-wire.json'

vi.mock('../../api', async () => {
  const real = await vi.importActual<typeof import('../../api/auth')>('../../api/auth')
  return { auditLogs: real.auditLogs, auth: { me: vi.fn() } }
})

const transport = vi.fn()
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
function serve(me: Record<string, unknown>, permissions: unknown) {
  transport.mockImplementation(async (url: string) => {
    if (url === '/api/auth/me') return json(me)
    if (url === '/api/permissions') return json(permissions)
    if (String(url).startsWith('/api/audit-logs')) return json(wire.audit_page)
    throw new Error('unexpected ' + url)
  })
}
async function openAudit(me: Record<string, unknown>) {
  await refreshPermissions()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], me)
  render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/audit']}><Routes>
    <Route path="/" element={<p>Dashboard home</p>} />
    <Route path="/audit" element={<RoleGate page="audit"><AuditLogs /></RoleGate>} />
  </Routes></MemoryRouter></QueryClientProvider>)
}
const granted = { ...wire.default_permissions, page_access: { ...wire.default_permissions.page_access, audit: ['admin'] } }
beforeEach(() => { vi.clearAllMocks(); vi.stubGlobal('fetch', transport) })
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('F19-19.1-02 audit log viewer is reachable by admins', () => {
  // RBAC_DEFAULT_PAGE_ACCESS now carries "audit": ["admin"], the key App.tsx/Sidebar RoleGate use.
  it('platform admin opens /audit with the default permission map', async () => {
    serve(wire.me, wire.default_permissions)
    await openAudit(wire.me)
    expect(await screen.findByRole('heading', { name: 'Audit Log' })).toBeInTheDocument()
  })

  it('operators are still redirected away from /audit with the default permission map', async () => {
    const operator = { ...wire.me, id: 6, username: 'operator1', role: 'operator', group_id: 1 }
    serve(operator, wire.default_permissions)
    await openAudit(operator)
    expect(await screen.findByText('Dashboard home')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Audit Log' })).not.toBeInTheDocument()
  })

  it('when the audit page is granted to admins, the platform admin sees timestamp, user, action and IP', async () => {
    serve(wire.me, granted)
    await openAudit(wire.me)
    expect(await screen.findByRole('heading', { name: 'Audit Log' })).toBeInTheDocument()
    const row = wire.audit_page.logs[0]
    expect(await screen.findByText(row.action)).toBeInTheDocument()
    expect(screen.getByText(row.username)).toBeInTheDocument()
    expect(screen.getByText(row.ip_address)).toBeInTheDocument()
    const stamp = new Date(row.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' })
    expect(screen.getByText(stamp)).toBeInTheDocument()
    const calls = transport.mock.calls.filter(([url]) => String(url).startsWith('/api/audit-logs'))
    expect(calls.length).toBeGreaterThan(0)
  })

  it('F19-19.1-03 Export CSV forwards the visible filters and downloads the real CSV body', async () => {
    serve(wire.me, granted)
    const base = transport.getMockImplementation()!
    transport.mockImplementation(async (url: string, init?: RequestInit) => String(url).startsWith('/api/export/audit-logs')
      ? new Response(wire.export_csv, { status: 200, headers: { 'Content-Type': 'text/csv; charset=utf-8' } }) : base(url, init))
    class OwnedURL extends URL { static createObjectURL = vi.fn(() => 'blob:certdops-audit'); static revokeObjectURL = vi.fn() }
    vi.stubGlobal('URL', OwnedURL)
    const downloads: string[] = []
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { downloads.push(this.download) })
    await openAudit(wire.me)
    await screen.findByText(wire.audit_page.logs[0].action)
    fireEvent.change(screen.getByPlaceholderText('Search logs...'), { target: { value: 'admin' } })
    fireEvent.change(screen.getByDisplayValue('All Actions'), { target: { value: 'create' } })
    fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }))
    await waitFor(() => expect(downloads).toHaveLength(1))
    expect(downloads[0]).toMatch(/^audit-logs-\d{4}-\d{2}-\d{2}\.csv$/)
    const [url] = transport.mock.calls.find(([u]) => String(u).startsWith('/api/export/audit-logs'))!
    const params = new URL(String(url), 'https://owned.invalid').searchParams
    expect(Object.fromEntries(params)).toEqual({ q: 'admin', action: 'create' })
    const blob = (OwnedURL.createObjectURL as any).mock.calls[0][0] as Blob
    expect(await blob.text()).toBe(wire.export_csv)
  })

  it('operators are redirected and never request audit history', async () => {
    const operator = { ...wire.me, id: 4, username: 'operator', role: 'operator' }
    serve(operator, granted)
    await openAudit(operator)
    expect(await screen.findByText('Dashboard home')).toBeInTheDocument()
    await waitFor(() => expect(transport.mock.calls.some(([url]) => String(url).startsWith('/api/audit-logs'))).toBe(false))
  })
})
