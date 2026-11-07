import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import AuditIntegritySettings from './AuditIntegritySettings'
const state = vi.hoisted(() => ({ licensed: true, group: null as number | null, role: 'admin' }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ hasFeature: () => state.licensed }) }))
vi.mock('../../permissions', () => ({ getCurrentUser: () => ({ role: state.role, group_id: state.group }) }))
function mount() {
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={cache}><AuditIntegritySettings /></QueryClientProvider>)
}
beforeEach(() => {
  state.licensed = true; state.group = null; state.role = 'admin'
  vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(url.endsWith('/verify') ? { ok: true, rows: 12 } : []), { status: 200 })))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
test('unlicensed users see Enterprise value and do not fetch', () => {
  state.licensed = false; mount()
  expect(screen.getByText(/require Enterprise/)).toBeTruthy()
  expect(fetch).not.toHaveBeenCalled()
})
test('org admin cannot load installation audit controls', () => {
  state.group = 4; mount()
  expect(screen.queryByText('Audit integrity & SIEM')).toBeNull()
  expect(fetch).not.toHaveBeenCalled()
})
test('verify and signed bundle date bounds are usable', async () => {
  mount(); await screen.findByText(/No destinations/)
  fireEvent.click(screen.getByText('Verify audit chain'))
  await screen.findByText('Verified 12 rows.')
  expect(screen.queryByText('Download signed JSON Lines')).toBeNull()
  fireEvent.change(screen.getByLabelText('Export from'), { target: { value: '2026-11-01' } })
  fireEvent.change(screen.getByLabelText('Export through'), { target: { value: '2026-11-05' } })
  expect(screen.getByText('Download signed JSON Lines').getAttribute('href')).toContain('bundle=true')
})
test('new destinations default disabled and save through canonical API', async () => {
  mount(); await screen.findByText(/No destinations/)
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Security' } })
  fireEvent.change(screen.getByLabelText('Transport'), { target: { value: 'syslog' } })
  fireEvent.change(screen.getByLabelText('Endpoint'), { target: { value: 'syslog://siem.example:6514' } })
  fireEvent.click(screen.getByText('Save destination'))
  await screen.findByText('Destination saved.')
  const call = vi.mocked(fetch).mock.calls.find(([, o]) => o?.method === 'PUT')!
  expect(call[0]).toBe('/api/settings/siem/new')
  expect(JSON.parse(String(call[1]?.body))).toEqual({ name: 'Security', kind: 'syslog', endpoint: 'syslog://siem.example:6514', enabled: false, tls: true })
})
test('delivery status and server errors are visible', async () => {
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify([{ id: 'abc', name: 'SOC', pending: 3, last_error: 'TimeoutError', last_success: null, enabled: 1 }]), { status: 200 }))
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Audit unavailable' }), { status: 503 }))
  mount(); await screen.findByText(/3 pending/)
  expect(screen.getByText(/TimeoutError/)).toBeTruthy()
  fireEvent.click(screen.getByText('Verify audit chain'))
  await waitFor(() => expect(screen.getByRole('alert').textContent).toBe('Audit unavailable'))
})
test('private CA bundle is included in HTTPS configuration', async () => {
  mount(); await screen.findByText(/No destinations/)
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Private SOC' } })
  fireEvent.change(screen.getByLabelText('Endpoint'), { target: { value: 'https://soc.example/events' } })
  fireEvent.change(screen.getByLabelText('Private CA bundle (optional PEM)'), { target: { value: 'PEM fixture' } })
  fireEvent.click(screen.getByText('Save destination'))
  await screen.findByText('Destination saved.')
  const call = vi.mocked(fetch).mock.calls.find(([, o]) => o?.method === 'PUT')!
  expect(JSON.parse(String(call[1]?.body)).ca_bundle).toBe('PEM fixture')
})
test('all integrity breaks are shown with known and new labels', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(url.endsWith('/verify') ? {
    ok: false, first_break: 1, reason: 'content', breaks: [
      { row_id: 1, reason: 'content', fingerprint: 'one', known: true },
      { row_id: 9, reason: 'link', fingerprint: 'nine', known: false },
    ],
  } : []), { status: 200 })))
  mount(); await screen.findByText(/No destinations/)
  fireEvent.click(screen.getByText('Verify audit chain'))
  await screen.findByText('Row 1: content (known)')
  expect(screen.getByText('Row 9: link (new)')).toBeTruthy()
})
test('syslog TLS configuration includes the private CA', async () => {
  mount(); await screen.findByText(/No destinations/)
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Syslog SOC' } })
  fireEvent.change(screen.getByLabelText('Transport'), { target: { value: 'syslog' } })
  fireEvent.change(screen.getByLabelText('Endpoint'), { target: { value: 'syslog://soc.example:6514' } })
  fireEvent.change(screen.getByLabelText('Private CA bundle (optional PEM)'), { target: { value: 'TLS PEM fixture' } })
  fireEvent.click(screen.getByText('Save destination'))
  await screen.findByText('Destination saved.')
  const call = vi.mocked(fetch).mock.calls.find(([, o]) => o?.method === 'PUT')!
  expect(JSON.parse(String(call[1]?.body)).ca_bundle).toBe('TLS PEM fixture')
})
test('missing audit keys are shown as recovery state without a tampering label', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(url.endsWith('/verify')
    ? { ok: false, status: 'audit_key_missing', breaks: [] } : []), { status: 200 })))
  mount(); await screen.findByText(/No destinations/)
  fireEvent.click(screen.getByText('Verify audit chain'))
  await screen.findByText(/Audit signing keys are missing/)
  expect(screen.queryByText(/Integrity failure at row/)).toBeNull()
  expect(screen.queryByText('Accept restore baseline')).toBeNull()
})
test('restore baseline acceptance sends the reason and refreshes verification', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(url.endsWith('/verify') ? {
    ok: false, breaks: [{ reason: 'signed_head_missing', row_id: null, known: true, fingerprint: 'restore' }],
  } : []), { status: 200 })))
  mount(); await screen.findByText(/No destinations/)
  fireEvent.click(screen.getByText('Verify audit chain'))
  await screen.findByText('Accept restore baseline')
  fireEvent.change(screen.getByLabelText('Restore acceptance reason'), { target: { value: 'Restored reviewed backup' } })
  fireEvent.click(screen.getByText('Accept restore baseline'))
  await screen.findByText(/Restore baseline accepted and recorded/)
  const call = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith('/accept-restore-baseline'))!
  expect(call[1]?.method).toBe('POST')
  expect(JSON.parse(String(call[1]?.body))).toEqual({ reason: 'Restored reviewed backup' })
})

test('missing-key initialization sends a reason and refreshes verification', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(url.endsWith('/verify')
    ? { ok: false, status: 'audit_key_missing', breaks: [] } : []), { status: 200 })))
  mount(); await screen.findByText(/No destinations/)
  fireEvent.click(screen.getByText('Verify audit chain'))
  const button = await screen.findByText('Initialize audit keyring')
  expect((button as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('Keyring initialization reason'), { target: { value: 'Original keys cannot be recovered' } })
  fireEvent.click(button)
  await screen.findByText(/Audit keyring initialized and alert recorded/)
  const call = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith('/initialize-keyring'))!
  expect(call[1]?.method).toBe('POST')
  expect(JSON.parse(String(call[1]?.body))).toEqual({ reason: 'Original keys cannot be recovered' })
  expect(vi.mocked(fetch).mock.calls.filter(([url]) => String(url).endsWith('/verify'))).toHaveLength(2)
})
test('standing acknowledgement confirms the saved receiver and sends the CSRF header', async () => {
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify([{ id: 'abc', name: 'SOC', endpoint: 'syslog://soc.example:6514', pending: 0, enabled: 0 }]), { status: 200 }))
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  mount(); const button = await screen.findByText('Acknowledge SOC')
  fireEvent.click(button)
  expect(vi.mocked(fetch).mock.calls.some(([, o]) => o?.method === 'POST')).toBe(false)
  confirm.mockReturnValue(true); fireEvent.click(button)
  await screen.findByText(/Destination acknowledged/)
  const call = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith('/acknowledge'))!
  expect(call[0]).toBe('/api/settings/siem/abc/acknowledge')
  expect(call[1]?.method).toBe('POST')
  expect(new Headers(call[1]?.headers).get('X-ODIN-Controlled-Ack')).toBe('true')
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining('syslog://soc.example:6514'))
  confirm.mockRestore()
})
test('standing acknowledgement displays who and when and disappears after target changes', async () => {
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify([{ id: 'abc', name: 'SOC', kind: 'syslog', endpoint: 'syslog://soc.example:6514', tls: true, pending: 0, enabled: 0,
    controlled_acknowledgement: { user_id: 4, acknowledged_at: '2026-11-06T12:00:00Z' } }]), { status: 200 }))
  mount(); await screen.findByText(/acknowledged by user 4 at 2026-11-06T12:00:00Z/)
  expect(screen.queryByText('Acknowledge SOC')).toBeNull()
  fireEvent.click(screen.getByText('Edit SOC'))
  fireEvent.change(screen.getByLabelText('Endpoint'), { target: { value: 'syslog://new.example:6514' } })
  vi.mocked(fetch).mockImplementation(async (url: string) => new Response(JSON.stringify(url.endsWith('/siem')
    ? [{ id: 'abc', name: 'SOC', endpoint: 'syslog://new.example:6514', pending: 0, enabled: 0, controlled_acknowledgement: null }] : {}), { status: 200 }))
  fireEvent.click(screen.getByText('Save destination'))
  await screen.findByText('Acknowledge SOC')
  expect(screen.queryByText(/acknowledged by user 4/)).toBeNull()
})

test('rotation saves explicit acknowledgement with CSRF while keeping delivery enabled', async () => {
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify([{ id: 'abc', name: 'SOC', kind: 'webhook', endpoint: 'https://soc.example/events', tls: true, pending: 0, enabled: 1,
    controlled_acknowledgement: { user_id: 4, acknowledged_at: '2026-11-06T12:00:00Z' } }]), { status: 200 }))
  mount(); fireEvent.click(await screen.findByText('Edit SOC'))
  fireEvent.change(screen.getByLabelText(/HMAC secret/), { target: { value: 'fixture-only-new-secret-0000000000' } })
  const consent = screen.getByLabelText(/I acknowledge this destination/)
  expect((consent as HTMLInputElement).checked).toBe(false)
  fireEvent.click(consent)
  fireEvent.click(screen.getByText('Save destination'))
  await screen.findByText('Destination saved.')
  const writes = vi.mocked(fetch).mock.calls.filter(([, o]) => o?.method === 'PUT')
  expect(writes).toHaveLength(1)
  expect(writes[0][0]).toBe('/api/settings/siem/abc')
  expect(JSON.parse(String(writes[0][1]?.body))).toMatchObject({ enabled: true, acknowledge_controlled: true, secret: 'fixture-only-new-secret-0000000000' })
  expect(new Headers(writes[0][1]?.headers).get('X-ODIN-Controlled-Ack')).toBe('true')
  expect(new Headers(writes[0][1]?.headers).get('Content-Type')).toBe('application/json')
  expect((consent as HTMLInputElement).checked).toBe(false)
})

test('target changes and switching destinations clear unsaved consent', async () => {
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify([{ id: 'abc', name: 'SOC', kind: 'webhook', endpoint: 'https://soc.example/events', tls: true, pending: 0, enabled: 1 }]), { status: 200 }))
  mount(); fireEvent.click(await screen.findByText('Edit SOC'))
  const consent = screen.getByLabelText(/I acknowledge this destination/) as HTMLInputElement
  for (const [label, value] of [['Endpoint', 'https://new.example/events'], [/HMAC secret/, 'fixture-only-new-secret-0000000000'], ['Private CA bundle (optional PEM)', 'PEM fixture']] as const) {
    fireEvent.click(consent); expect(consent.checked).toBe(true)
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
    expect(consent.checked).toBe(false)
  }
  fireEvent.click(consent)
  fireEvent.click(screen.getByText('Cancel edit'))
  expect(consent.checked).toBe(false)
  fireEvent.click(consent)
  fireEvent.click(screen.getByText('Edit SOC'))
  expect(consent.checked).toBe(false)
})

test('rotation refusal preserves the enabled form and shows the server reason', async () => {
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify([{ id: 'abc', name: 'SOC', kind: 'webhook', endpoint: 'https://soc.example/events', tls: true, pending: 0, enabled: 1 }]), { status: 200 }))
  mount(); fireEvent.click(await screen.findByText('Edit SOC'))
  fireEvent.change(screen.getByLabelText('Endpoint'), { target: { value: 'https://new.example/events' } })
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ detail: { code: 'controlled_part_siem_acknowledgement_required', message: 'A superadmin must acknowledge SIEM destination SOC.' } }), { status: 403 }))
  fireEvent.click(screen.getByText('Save destination'))
  expect((await screen.findByRole('alert')).textContent).toBe('A superadmin must acknowledge SIEM destination SOC.')
  expect((screen.getByLabelText('Endpoint') as HTMLInputElement).value).toBe('https://new.example/events')
  expect((screen.getByLabelText('Enable delivery of new audit events') as HTMLInputElement).checked).toBe(true)
  const call = vi.mocked(fetch).mock.calls.find(([, o]) => o?.method === 'PUT')!
  expect(JSON.parse(String(call[1]?.body)).acknowledge_controlled).toBeUndefined()
  expect(new Headers(call[1]?.headers).get('X-ODIN-Controlled-Ack')).toBeNull()
})

test('erased acknowledgement author remains visibly acknowledged', async () => {
  vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify([{ id: 'abc', name: 'SOC', pending: 0, enabled: 1,
    controlled_acknowledgement: { user_id: null, acknowledged_at: '2026-11-06T12:00:00Z' } }]), { status: 200 }))
  mount(); await screen.findByText(/acknowledged by an erased user at 2026-11-06T12:00:00Z/)
  expect(screen.queryByText('Acknowledge SOC')).toBeNull()
})
