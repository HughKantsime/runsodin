import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import UnaccountedFilament from './UnaccountedFilament'
const state = vi.hoisted(() => ({ allowed: true }))
vi.mock('../../permissions', () => ({ canDo: () => state.allowed, clearSensitiveBrowserState: vi.fn() }))
const fetcher = vi.fn()
beforeEach(() => {
  state.allowed = true; fetcher.mockReset()
  fetcher.mockResolvedValue(new Response(JSON.stringify({ unresolved: 2, by_cause: [{ title: 'The spool has no known balance' }] }), { status: 200 }))
  vi.stubGlobal('fetch', fetcher)
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
function mount() { render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><UnaccountedFilament /></MemoryRouter></QueryClientProvider>) }
test('real query links the count and top cause to filtered archives', async () => {
  mount(); const link = await screen.findByRole('link')
  expect(link).toHaveAttribute('href', '/archives?accounting=unresolved')
  expect(link).toHaveTextContent('2 finished prints need their filament settled')
  expect(screen.getByText(/The spool has no known balance/)).toBeInTheDocument()
  expect(fetcher).toHaveBeenCalledWith('/api/archives/accounting-summary?days=30', expect.anything())
})
test.each(['denied', 'empty', 'failure', 'loading'])('renders nothing when %s', async reason => {
  if (reason === 'denied') state.allowed = false
  if (reason === 'empty') fetcher.mockResolvedValue(new Response(JSON.stringify({ unresolved: 0, by_cause: [] })))
  if (reason === 'failure') fetcher.mockRejectedValue(new TypeError('Connection lost'))
  if (reason === 'loading') fetcher.mockReturnValue(new Promise(() => {}))
  mount()
  if (reason !== 'denied') await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
  if (reason === 'denied') expect(fetcher).not.toHaveBeenCalled()
})
