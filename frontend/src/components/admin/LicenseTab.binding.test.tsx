import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import LicenseTab from './LicenseTab'

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

function setup(bound = false, failure = false, group: number | null = null, valid = true) {
  const transport = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/license' && !init?.method) return json({ tier: 'pro', valid, device_bound: bound })
    if (url === '/api/license/installation-id') return json({ installation_id: 'synthetic-id', device_fingerprint: '0123 4567 89ab cdef' })
    if (url === '/api/license/activate') return failure ? json({ detail: { code: 'DEVICE_MISMATCH', message: 'Device key mismatch. Restore the original installation ID and device key, or contact support for an authorized re-issue.' } }, 409) : json({ tier: 'pro' })
    return json({ detail: 'Unexpected call' }, 500)
  })
  vi.stubGlobal('fetch', transport)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 1, role: 'admin', group_id: group })
  render(<QueryClientProvider client={client}><LicenseTab /></QueryClientProvider>)
  return transport
}
const activateCalls = (transport: ReturnType<typeof setup>) => transport.mock.calls.filter(([url]) => url === '/api/license/activate')

test('bound status and preservation guidance render without conversion', async () => {
  setup(true)
  expect(await screen.findByText('Device-bound license')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Convert purchase to device binding' })).not.toBeInTheDocument()
  expect(screen.getByText(/Restoring application data alone/)).toBeInTheDocument()
})

test('conversion requires displayed fingerprint and explicit purchase-wide confirmation', async () => {
  const transport = setup()
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  expect(await screen.findByText('Legacy license')).toBeInTheDocument()
  fireEvent.change(screen.getByRole('textbox', { name: 'License key' }), { target: { value: 'TEST-ONLY' } })
  const convert = screen.getByRole('button', { name: 'Convert purchase to device binding' })
  expect(convert).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Show installation ID' }))
  await screen.findByText('0123 4567 89ab cdef')
  expect(convert).toBeEnabled()
  fireEvent.click(convert)
  expect(activateCalls(transport)).toHaveLength(0)
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining('whole purchase'))
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining('0123 4567 89ab cdef'))
  confirm.mockReturnValue(true)
  fireEvent.click(convert)
  await waitFor(() => expect(activateCalls(transport)).toHaveLength(1))
  const request = JSON.parse(activateCalls(transport)[0][1]!.body as string)
  expect(request).toEqual({ key: 'TEST-ONLY', binding_protocol: 'device-v1', convert_to_device_binding: true, confirm_purchase_wide: true })
})

test('explicit bound activation reports actionable mismatch and retains status', async () => {
  const transport = setup(false, true)
  await screen.findByText('Legacy license')
  fireEvent.change(screen.getByRole('textbox', { name: 'License key' }), { target: { value: 'TEST-ONLY' } })
  fireEvent.click(screen.getByRole('checkbox', { name: /Request device-bound activation/ }))
  fireEvent.click(screen.getByRole('button', { name: /^Activate$/ }))
  await screen.findByText(/Activation failed: Device key mismatch/)
  expect(screen.getByText('Legacy license')).toBeInTheDocument()
  expect(JSON.parse(activateCalls(transport)[0][1]!.body as string).binding_protocol).toBe('device-v1')
})

test('organization administrator does not get purchase conversion', async () => {
  setup(false, false, 7)
  await screen.findByText('Legacy license')
  expect(screen.queryByRole('button', { name: 'Convert purchase to device binding' })).not.toBeInTheDocument()
})


test('an invalid bound license retains its binding status and recovery guidance', async () => {
  setup(true, false, null, false)
  expect(await screen.findByText('Device-bound license')).toBeInTheDocument()
  expect(screen.getByText(/For a device mismatch, restore both originals or contact support/)).toBeInTheDocument()
})
