import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Printers from './Printers'
import { applyTheme } from '../../hooks/themePreferences'
import { printers, filaments } from '../../api'
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, atPrinterLimit: () => false }) }))
vi.mock('../../permissions', () => ({ canDo: () => true, canAccessPage: () => true, getCurrentUser: () => ({ role: 'admin' }) }))
beforeEach(() => { sessionStorage.clear(); localStorage.clear() })
afterEach(() => { cleanup(); vi.restoreAllMocks(); applyTheme('dark', 'classic') })
it.each(['classic', 'foundry'] as const)('%s catalogue choice refreshes through real parents without a slot PATCH', async family => {
  applyTheme('dark', family)
  const choose = vi.spyOn(filaments, 'chooseSlot').mockResolvedValue({ success: true })
  const patch = vi.spyOn(printers, 'updateSlot').mockResolvedValue({} as any)
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false }, mutations: { retry: false } } })
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  client.setQueryData(['auth', 'me'], { role: 'admin' })
  client.setQueryData(['printers', null], [{ id: 1, name: 'Review printer', api_type: 'moonraker', is_active: true, last_seen: new Date().toISOString(), gcode_state: 'IDLE', tags: [],
    filament_slots: [{ id: 1, slot_number: 1, filament_type: 'PLA', material_type: 'PLA', color_hex: '1234AB', remaining: null, assigned_spool_id: 7, catalogue_choices: [{ id: 12, name: 'Invented Orbit' }] }] }])
  render(<QueryClientProvider client={client}><MemoryRouter><Printers /></MemoryRouter></QueryClientProvider>)
  fireEvent.click(screen.getByTestId('filament-slot'))
  fireEvent.click(screen.getByRole('button', { name: 'Invented Orbit' }))
  await waitFor(() => expect(choose).toHaveBeenCalledWith(1, 1, 7, 12))
  await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['printers'] }))
  expect(patch).not.toHaveBeenCalled()
})
