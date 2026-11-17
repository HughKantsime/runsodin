import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import Archives from './Archives'
import { fetchAPI } from '../../api/client'
vi.mock('../../permissions', () => ({ canDo: () => false, getCurrentUser: () => ({ role: 'viewer' }), clearSensitiveBrowserState: vi.fn() }))
const fetcher = vi.fn()
beforeEach(() => {
  fetcher.mockReset().mockImplementation(async (url: string) => new Response(JSON.stringify(url.startsWith('/api/archives?') ? { items: [], total: 0 } : [])))
  vi.stubGlobal('fetch', fetcher)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })
function mount(path: string) { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={[path]}><Archives /></MemoryRouter></QueryClientProvider>) }
test('URL initialises the accounting filter and the real request carries it', async () => {
  mount('/archives?accounting=unresolved')
  expect(screen.getByLabelText('Filter by filament accounting')).toHaveValue('unresolved')
  await screen.findByText('No prints match these filters')
  expect(fetcher.mock.calls.some(([url]) => url.startsWith('/api/archives?') && new URL(url, 'http://localhost').searchParams.get('accounting') === 'unresolved')).toBe(true)
})
test('invalid URL filter shows All and does not send an invalid accounting value', async () => {
  mount('/archives?accounting=junk')
  expect(screen.getByLabelText('Filter by filament accounting')).toHaveValue('')
  await screen.findByText('No archived prints yet')
  expect(fetcher.mock.calls.filter(([url]) => url.startsWith('/api/archives?')).every(([url]) => !new URL(url, 'http://localhost').searchParams.has('accounting'))).toBe(true)
})
test('real fetchAPI preserves the 409 object message and status', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {})
  fetcher.mockResolvedValue(new Response(JSON.stringify({ detail: { code: 'already_deducted', message: 'Keep the existing deduction.' } }), { status: 409 }))
  await expect(fetchAPI('/archives/1/settle-filament', { method: 'POST' })).rejects.toMatchObject({ message: 'Keep the existing deduction.', status: 409, detail: { code: 'already_deducted' } })
})
