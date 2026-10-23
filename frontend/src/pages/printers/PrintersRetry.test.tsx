import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import Printers from './Printers'
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, atPrinterLimit: () => false }) }))
vi.mock('../../permissions', () => ({ canDo: () => false, getCurrentUser: () => null }))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
it('shows HTTP 500 and retries to a successful list', async () => {
 let attempts = 0
 vi.stubGlobal('fetch', vi.fn(async (url: string) => {
  if (String(url).match(/\/printers(?:\?|$)/)) {
   attempts++
   return new Response(attempts === 1 ? JSON.stringify({ detail: 'HTTP 500' }) : '[]', { status: attempts === 1 ? 500 : 200, headers: { 'Content-Type': 'application/json' } })
  }
  return new Response('[]', { headers: { 'Content-Type': 'application/json' } })
 }))
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
 render(<QueryClientProvider client={client}><MemoryRouter><Printers /></MemoryRouter></QueryClientProvider>)
 expect(await screen.findByRole('alert')).toHaveTextContent('Failed to load printers')
 fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
 expect(await screen.findByText('No printers configured yet.')).toBeInTheDocument()
 expect(attempts).toBe(2)
})
