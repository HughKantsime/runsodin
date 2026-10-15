// F20-20.2-06 / F20-20.3-05: offline (air-gapped) activation and unactivation
// requests are produced by the real LicenseTab through the real API client.
// Only the network boundary (fetch) is stubbed; the backend half of the same
// call is pinned by tests/test_contracts/test_cert_G_license_flows.py.
import { cleanup, fireEvent, render as rtlRender, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import LicenseTab from './LicenseTab'

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

let transport: ReturnType<typeof vi.fn>
let downloads: string[]

function render(licenseBody: Record<string, unknown>) {
  transport = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/license' && (!init?.method || init.method === 'GET')) return json(licenseBody)
    if (url === '/api/license/activation-request') return json({ key: 'ODIN-TEST-AAAA-BBBB', activation_request: { installation_id: 'inst-1', nonce: 'n1' } })
    if (url === '/api/license/unactivation-request') return json({ key: 'ODIN-TEST-AAAA-BBBB', installation_id: 'inst-1', nonce: 'n2', signature: 'c2ln' })
    return json({ detail: 'unexpected ' + url }, 500)
  })
  vi.stubGlobal('fetch', transport)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 3, role: 'admin', group_id: 1 })
  return rtlRender(<QueryClientProvider client={client}><LicenseTab /></QueryClientProvider>)
}

beforeEach(() => {
  downloads = []
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:synthetic')
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { downloads.push(this.download) })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

const calls = (path: string) => transport.mock.calls.filter(([u]) => u === path)

test('activation request is POSTed with the license key and the portal challenge code', async () => {
  render({ tier: 'community', valid: false, managed_externally: false })
  const button = await screen.findByRole('button', { name: 'Download activation request' })
  expect(button).toBeDisabled()
  fireEvent.change(screen.getByPlaceholderText('ODIN-XXXX-XXXX-XXXX'), { target: { value: ' ODIN-TEST-AAAA-BBBB ' } })
  expect(button).toBeDisabled()
  fireEvent.change(screen.getByRole('textbox', { name: 'Challenge code' }), { target: { value: ' n1 ' } })
  expect(button).toBeEnabled()
  fireEvent.click(button)
  await screen.findByText(/Activation request downloaded/)
  const [[, init]] = calls('/api/license/activation-request')
  expect(init.method).toBe('POST')
  expect(JSON.parse(init.body as string)).toEqual({ key: 'ODIN-TEST-AAAA-BBBB', nonce: 'n1' })
  expect(downloads).toEqual(['odin-activation-request.json'])
})

test('unactivation request is POSTed with the challenge code and offered only for an installed license', async () => {
  const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
  const view = render({ tier: 'pro', valid: true, managed_externally: false })
  const button = await screen.findByRole('button', { name: 'Download unactivation request' })
  fireEvent.change(screen.getByRole('textbox', { name: 'Challenge code' }), { target: { value: 'n2' } })
  fireEvent.click(button)
  await screen.findByText(/Unactivation request downloaded/)
  const [[, init]] = calls('/api/license/unactivation-request')
  expect(init.method).toBe('POST')
  expect(JSON.parse(init.body as string)).toEqual({ nonce: 'n2' })
  expect(downloads).toEqual(['odin-unactivation-request.json'])
  expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining('removes the license from this server'))
  // The removed license is reflected at once (GET /api/license re-read).
  await waitFor(() => expect(calls('/api/license').length).toBe(2))
  view.unmount()

  render({ tier: 'community', valid: false, managed_externally: false })
  await screen.findByRole('button', { name: 'Download activation request' })
  expect(screen.queryByRole('button', { name: 'Download unactivation request' })).not.toBeInTheDocument()
})

test('a rejected request shows the backend reason and downloads nothing', async () => {
  render({ tier: 'community', valid: false, managed_externally: false })
  await screen.findByRole('button', { name: 'Download activation request' })
  transport.mockImplementation(async () => json({ detail: 'A license key and a challenge nonce from the licensing server are required.' }, 400))
  fireEvent.change(screen.getByPlaceholderText('ODIN-XXXX-XXXX-XXXX'), { target: { value: 'ODIN-TEST-AAAA-BBBB' } })
  fireEvent.change(screen.getByRole('textbox', { name: 'Challenge code' }), { target: { value: 'n1' } })
  fireEvent.click(screen.getByRole('button', { name: 'Download activation request' }))
  await waitFor(() => expect(screen.getByText(/Failed to export activation request: A license key and a challenge nonce/)).toBeInTheDocument())
  expect(downloads).toEqual([])
})

test('declining the confirmation sends no unactivation request', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(false)
  render({ tier: 'pro', valid: true, managed_externally: false })
  const button = await screen.findByRole('button', { name: 'Download unactivation request' })
  fireEvent.change(screen.getByRole('textbox', { name: 'Challenge code' }), { target: { value: 'n2' } })
  fireEvent.click(button)
  expect(calls('/api/license/unactivation-request')).toHaveLength(0)
  expect(downloads).toEqual([])
})
