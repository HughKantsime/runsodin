// cert-D jobsA F04-4.6-01: real GeneralTab ApprovalToggle + real API client + real RoleGate/permissions,
// replaying the producer wire captured by
// tests/test_contracts/test_cert_D_jobsA_approval_workflow.py::test_capture_certD_jobsA_approval_wire.
// Only window.fetch and the installation-license context (server boundary) are substituted.
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import GeneralTab from './GeneralTab'
import RoleGate from '../auth/RoleGate'
import { refreshPermissions, clearSensitiveBrowserState } from '../../permissions'
import wire from '../../test-fixtures/certD-jobsA-approval-wire.json'

const lic = vi.hoisted(() => ({ features: ['job_approval'] as string[] }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, isEducation: true, hasFeature: (f: string) => lic.features.includes(f), loading: false }) }))

type Call = { method: string; url: string; body: unknown }
let calls: Call[] = []
let principal: keyof typeof wire.me = 'admin'
let setting: unknown = wire.approval_setting

function reply(status: number, body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

beforeEach(() => {
  calls = []; principal = 'admin'; setting = wire.approval_setting; lic.features = ['job_approval']
  vi.stubGlobal('fetch', vi.fn((input: string, init: RequestInit = {}) => {
    const method = (init.method || 'GET').toUpperCase()
    const url = String(input)
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined
    calls.push({ method, url, body })
    if (url === '/api/auth/me') return reply(200, wire.me[principal])
    if (url === '/api/permissions') return reply(200, wire.permissions)
    if (url === '/api' + wire.approval_put.path && method === 'GET') return reply(200, setting)
    if (url === '/api' + wire.approval_put.path && method === 'PUT') { setting = wire.approval_after_put; return reply(wire.approval_put.status, wire.approval_put.body) }
    if (method === 'GET') return reply(200, {})
    return reply(500, { detail: 'unrecorded' })
  }))
})
afterEach(async () => { cleanup(); await clearSensitiveBrowserState(); vi.unstubAllGlobals() })

describe('F04-4.6-01 approval toggle in Settings (admin-only)', () => {
  it.each([['admin', true], ['operator1', false], ['student', false]] as const)('Settings route for %s is reachable: %s', async (user, allowed) => {
    principal = user
    await refreshPermissions()
    render(<MemoryRouter initialEntries={['/settings']}><Routes>
      <Route path="/settings" element={<RoleGate page="settings"><div>Settings content</div></RoleGate>} />
      <Route path="/" element={<div>Home</div>} />
    </Routes></MemoryRouter>)
    expect(screen.queryByText('Settings content') != null).toBe(allowed)
    expect(screen.queryByText('Home') != null).toBe(!allowed)
  })

  it('admin toggle reflects the real setting and sends the recorded PUT, then shows the refreshed state', async () => {
    principal = 'admin'; await refreshPermissions()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><GeneralTab /></QueryClientProvider>)
    expect(await screen.findByText('Job Approval Workflow')).toBeInTheDocument()
    const approvalSwitch = () => screen.getByText(/^Approval (required|disabled)/).closest('label')!.querySelector('[role=switch]')!
    await screen.findByText('Approval required for viewer-role users')
    expect(wire.approval_setting.require_job_approval).toBe(true)
    expect(approvalSwitch()).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(approvalSwitch())
    await waitFor(() => expect(calls.find(c => c.method === 'PUT')?.body).toEqual(wire.approval_put.request))
    expect(calls.find(c => c.method === 'PUT')?.url).toBe('/api' + wire.approval_put.path)
    await screen.findByText('Approval disabled — all users create jobs directly')
    expect(approvalSwitch()).toHaveAttribute('aria-checked', 'false')
  })

  it('claim label "Require Job Approval" is not what the Settings UI shows (claim wording check)', async () => {
    principal = 'admin'; await refreshPermissions()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><GeneralTab /></QueryClientProvider>)
    await screen.findByText('Job Approval Workflow')
    expect(screen.queryByText(/Require Job Approval/i)).not.toBeInTheDocument()
  })

  it('toggle is absent without the job_approval license feature', async () => {
    lic.features = []; principal = 'admin'; await refreshPermissions()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><GeneralTab /></QueryClientProvider>)
    await new Promise(r => setTimeout(r, 0))
    expect(screen.queryByText('Job Approval Workflow')).not.toBeInTheDocument()
    expect(screen.queryByText(/^Approval (required|disabled)/)).not.toBeInTheDocument()
  })
})
