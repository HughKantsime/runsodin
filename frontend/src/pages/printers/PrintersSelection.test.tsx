import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Printers from './Printers'
import { applyTheme } from '../../hooks/themePreferences'
import { bulkOps } from '../../api'
import { bulkNames, testBulkUpdatePage } from '../../test/bulkUpdateCases'

const notices = vi.hoisted(() => ({ partial: vi.fn(), success: vi.fn(), error: vi.fn() }))
vi.mock('react-hot-toast', () => ({ default: Object.assign(notices.partial, notices) }))
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, atPrinterLimit: () => false }) }))
vi.mock('../../permissions', () => ({ canDo: () => true, canAccessPage: () => true, getCurrentUser: () => null }))
beforeEach(() => { sessionStorage.clear(); localStorage.clear() })
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.clearAllMocks(); applyTheme('dark', 'classic') })
it.each(['classic', 'foundry'] as const)('%s printer selection retains its name and bulk-selection behavior', family => {
  applyTheme('dark', family)
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
  client.setQueryData(['auth', 'me'], { role: 'operator' })
  client.setQueryData(['printers', null], [{ id: 1, name: 'Review printer', nickname: 'Workshop', model: 'Synthetic model', api_type: 'bambu', is_active: true, last_seen: new Date().toISOString(), gcode_state: 'IDLE', print_stage: 'Idle', filament_slots: [], tags: [] }])
  render(<QueryClientProvider client={client}><MemoryRouter><Printers /></MemoryRouter></QueryClientProvider>)
  const checkbox = screen.getByRole('checkbox', { name: 'Select Workshop' })
  if (family === 'foundry') {
    const card = screen.getByRole('article', { name: 'Workshop' })
    expect(within(card).getByRole('checkbox', { name: 'Select Workshop' })).toBe(checkbox)
    expect(checkbox.closest('.f-printer-heading')).toContainElement(within(card).getByRole('heading', { name: 'Workshop', level: 2 }))
    expect(checkbox.closest('label')).toHaveClass('f-check-target')
  } else {
    expect(checkbox.closest('label')).toBeNull()
    expect(checkbox).toHaveClass('absolute', 'top-3', 'left-3', 'z-10')
    expect(checkbox.nextElementSibling).toHaveAttribute('data-testid', 'printer-card')
  }
  fireEvent.click(checkbox)
  expect(checkbox).toBeChecked()
  expect(screen.getByText('1 selected')).toBeInTheDocument()
  fireEvent.click(checkbox)
  expect(checkbox).not.toBeChecked()
})

testBulkUpdatePage({
  item: 'printer', action: 'Disable', notices,
  checkbox: name => screen.getByRole('checkbox', { name: `Select ${name}` }),
  mount: async (affected, skips) => {
    vi.spyOn(bulkOps, 'printers').mockResolvedValue({ status: 'ok', affected, skipped: skips.map(({ id, detail }) => ({ printer_id: id, detail })) })
    const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
    client.setQueryData(['auth', 'me'], { role: 'admin' })
    client.setQueryData(['printers', null], bulkNames.map((name, i) => ({ id: 41 + i, name, api_type: 'bambu', is_active: true, filament_slots: [], tags: [] })))
    render(<QueryClientProvider client={client}><MemoryRouter><Printers /></MemoryRouter></QueryClientProvider>)
    await screen.findByRole('checkbox', { name: 'Select Birch' })
  },
})
