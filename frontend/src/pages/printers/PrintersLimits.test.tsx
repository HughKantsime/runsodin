import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import Printers from './Printers'
import { LicenseProvider } from '../../LicenseContext'
import { license as licenseApi } from '../../api'
import publicWire from '../../test-fixtures/public-license-wire-owned.json'
vi.mock('../../api', async importOriginal => { const actual = await importOriginal<typeof import('../../api')>(); return { ...actual, license: { ...actual.license, get: vi.fn() } } })
vi.mock('../../permissions', () => ({ canDo: () => true, getCurrentUser: () => null }))
afterEach(() => { cleanup(); sessionStorage.clear(); localStorage.clear(); vi.clearAllMocks() })
it.each<[string, number | null, number, boolean]>([
  ['pro', null, 6, false], ['pro', 0, 0, true], ['pro', 2, 2, true],
  ['pro', 374, 2, false], ['pro', 9999, 2, false],
  ['education', 2, 2, true], ['community', 5, 5, true],
])('Add Printer respects resolved %s cap %s at count %s', async (tier, cap, count, blocked) => {
  vi.mocked(licenseApi.get).mockResolvedValue({ ...publicWire.body, tier, max_printers: cap, max_users: null, features: [] })
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
  client.setQueryData(['printers', null], Array.from({length: count}, (_, index) => ({
    id: index + 1, name: `Synthetic ${index}`, api_type: 'bambu', is_active: true,
    gcode_state: 'IDLE', last_seen: new Date().toISOString(), filament_slots: [], tags: [],
  })))
  render(<QueryClientProvider client={client}><MemoryRouter><LicenseProvider><Printers /></LicenseProvider></MemoryRouter></QueryClientProvider>)
  await waitFor(() => expect(licenseApi.get).toHaveBeenCalledWith())
  await waitFor(() => {
    if (blocked) expect(screen.getByRole('button', { name: `Add Printer (limit: ${cap})` })).toBeInTheDocument()
    else expect(screen.getByRole('button', { name: 'Add Printer', exact: true })).toBeInTheDocument()
  })
})
