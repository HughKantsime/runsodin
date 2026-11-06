import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import toast, { Toaster } from 'react-hot-toast'
import Printers from './Printers'
import { printers } from '../../api/printers'

vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, atPrinterLimit: () => false }) }))
vi.mock('../../permissions', () => ({ canDo: () => true, canAccessPage: () => true, getCurrentUser: () => null }))
beforeEach(() => {
  sessionStorage.clear(); localStorage.clear()
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn() })))
})
afterEach(() => { cleanup(); toast.remove(); vi.unstubAllGlobals() })

it('deletes through the confirmation UI and displays warnings from an empty 204 response', async () => {
  const warning = 'Training no longer matches any printer: Safety — école. Review its training scope.'
  const fetchMock = vi.fn(async () => new Response(null, { status: 204, headers: { 'X-ODIN-Training-Warnings': JSON.stringify([warning]).replace(/[^\x00-\x7f]/g, char => '\\u' + char.charCodeAt(0).toString(16).padStart(4, '0')) } }))
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
  client.setQueryData(['auth', 'me'], { role: 'admin' })
  client.setQueryData(['printers', null], [{ id: 11, name: 'School printer', model: 'Synthetic model', api_type: 'bambu', is_active: true, last_seen: new Date().toISOString(), gcode_state: 'IDLE', print_stage: 'Idle', filament_slots: [], tags: [] }])
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  render(<QueryClientProvider client={client}><MemoryRouter><Printers /><Toaster /></MemoryRouter></QueryClientProvider>)
  fireEvent.click(screen.getByRole('button', { name: 'Delete printer' }))
  expect(fetchMock).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Delete', exact: true }))
  expect(await screen.findByText(warning)).toBeInTheDocument()
  expect(fetchMock).toHaveBeenCalledWith('/api/printers/11', expect.objectContaining({ method: 'DELETE', credentials: 'include' }))
  await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['printers'] }))
})

it.each([null, 'not-json', '{}', '[1,null]', '[]'])('handles 204 with absent or malformed warnings: %s', async header => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 204, headers: header === null ? {} : { 'X-ODIN-Training-Warnings': header } })))
  expect(await printers.delete(11)).toEqual({ training_warnings: [] })
})

it('does not treat an error response as successful deletion', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'Printer not found' }), { status: 404 })))
  await expect(printers.delete(11)).rejects.toThrow('Printer not found')
})
