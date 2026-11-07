import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Setup from './Setup'

vi.mock('../../permissions', () => ({ refreshPermissions: vi.fn().mockResolvedValue(undefined) }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ hasFeature: () => false }) }))
let request: ReturnType<typeof vi.fn>
beforeEach(() => {
  request = vi.fn(async (url: string) => {
    if (url === '/api/setup/admin' || url === '/api/auth/login') {
      return { ok: true, json: async () => ({ access_token: 'setup-test-token' }) }
    }
    if (url === '/api/license/upload') return { ok: true, json: async () => ({ tier: 'pro', licensee: 'Offline test' }) }
    throw new Error(`Unexpected request: ${url}`)
  })
  vi.stubGlobal('fetch', request)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

async function selectFile() {
  const { container } = render(<MemoryRouter><Setup /></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: /Get Started/ }))
  fireEvent.change(screen.getByPlaceholderText('admin'), { target: { value: 'admin' } })
  fireEvent.change(screen.getByPlaceholderText('you@example.com'), { target: { value: 'admin@example.test' } })
  fireEvent.change(screen.getByPlaceholderText(/Min 8 characters/), { target: { value: 'SetupTest123!' } })
  fireEvent.change(screen.getByPlaceholderText('Repeat password'), { target: { value: 'SetupTest123!' } })
  fireEvent.click(screen.getByRole('button', { name: /Create Account/ }))
  fireEvent.click(await screen.findByRole('button', { name: /Offline activation/ }))
  const file = new File(['{"payload":"synthetic-payload","signature":"synthetic-signature"}'], 'offline.json', { type: 'application/json' })
  fireEvent.change(container.querySelector('input[type=file]')!, { target: { files: [file] } })
  return file
}

test('setup sends signed file as multipart with its token and advances on success', async () => {
  const file = await selectFile()
  fireEvent.click(screen.getByRole('button', { name: 'Activate' }))
  expect(await screen.findByRole('heading', { name: 'Add a Printer' })).toBeInTheDocument()
  const calls = request.mock.calls.filter(([url]) => url === '/api/license/upload')
  expect(calls).toHaveLength(1)
  const options = (calls[0] as unknown as [string, RequestInit])[1]
  expect(options.method).toBe('POST')
  expect(options.credentials).toBe('include')
  expect(options.headers).toEqual({ Authorization: 'Bearer setup-test-token' })
  expect(options.body).toBeInstanceOf(FormData)
  expect((options.body as FormData).get('file')).toBe(file)
})

test('invalid signed file displays backend error and stays on tier step', async () => {
  await selectFile()
  request.mockResolvedValueOnce({ ok: false, json: async () => ({ detail: 'Invalid license signature' }) })
  fireEvent.click(screen.getByRole('button', { name: 'Activate' }))
  expect(await screen.findByText('Invalid license signature')).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Pick a Tier' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Activate' })).toBeEnabled()
})

test('upload failure remains visible and can be retried', async () => {
  await selectFile()
  request.mockRejectedValueOnce(new Error('Connection interrupted'))
  fireEvent.click(screen.getByRole('button', { name: 'Activate' }))
  expect(await screen.findByText('Connection interrupted')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Activate' }))
  expect(await screen.findByRole('heading', { name: 'Add a Printer' })).toBeInTheDocument()
})
