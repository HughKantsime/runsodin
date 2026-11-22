import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Printers from './Printers'
import { applyTheme } from '../../hooks/themePreferences'

vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, atPrinterLimit: () => false }) }))
vi.mock('../../permissions', () => ({ canDo: () => true, canAccessPage: () => false, getCurrentUser: () => null }))
beforeEach(() => { sessionStorage.clear(); localStorage.clear() })
afterEach(() => { cleanup(); applyTheme('dark', 'classic') })

it.each(['classic', 'foundry'] as const)('%s shows the pause cause once on the Printers card', family => {
  applyTheme('dark', family)
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
  client.setQueryData(['auth', 'me'], { role: 'operator' })
  client.setQueryData(['printers', null], [{ id: 1, name: 'Paused printer', model: 'Synthetic model', api_type: 'bambu', is_active: true, last_seen: new Date().toISOString(), gcode_state: 'PAUSE', print_stage: 'Paused: AMS runout', last_error_message: 'Stale fan error', filament_slots: [], tags: [] }])
  render(<QueryClientProvider client={client}><MemoryRouter><Printers /></MemoryRouter></QueryClientProvider>)
  const cause = screen.getByText('Paused: AMS runout')
  expect(cause).toBeVisible()
  expect(cause).toHaveAttribute('role', 'status')
  expect(screen.queryByText('Stale fan error')).not.toBeInTheDocument()
  if (family === 'foundry') {
    const card = screen.getByRole('article', { name: 'Paused printer' })
    const summary = within(card).getByText('Controls and settings')
    const disclosure = summary.closest('details')!
    expect(disclosure).not.toHaveAttribute('open')
    expect(within(disclosure).queryByText('Paused: AMS runout')).not.toBeInTheDocument()
    expect(cause.closest('details')).toBeNull()
    expect(cause.parentElement).toBe(card)
    expect(cause).toHaveClass('f-pause-cause')
    fireEvent.click(summary)
    expect(disclosure).toHaveAttribute('open')
    expect(screen.getAllByText('Paused: AMS runout')).toHaveLength(1)
  } else {
    expect(cause.parentElement).toBe(screen.getByTestId('printer-card'))
    expect(cause).toHaveAttribute('class', 'px-3 py-2 text-sm text-amber-500')
    expect(cause.nextElementSibling).toHaveClass('filament-slot-container')
    expect(screen.queryByText('Controls and settings')).not.toBeInTheDocument()
  }
})

it('keeps one rack inventory row when Foundry controls open and close', async () => {
  applyTheme('dark', 'foundry')
  const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
  client.setQueryData(['auth', 'me'], { role: 'operator' })
  client.setQueryData(['printers', null], [{ id: 1, name: 'Rack', model: 'H3C', api_type: 'bambu',
    is_active: true, tags: [], filament_slots: [{ id: 7, slot_number: 7, filament_type: 'PLA', remaining: 83 }] }])
  render(<QueryClientProvider client={client}><MemoryRouter><Printers /></MemoryRouter></QueryClientProvider>)
  const controls = screen.getByText('Controls and settings')
  expect(screen.getAllByText(/\bPLA\b/)).toHaveLength(1)
  fireEvent.click(controls)
  expect(await screen.findByTestId('filament-slot')).toBeVisible()
  expect(screen.getAllByText(/\bPLA\b/)).toHaveLength(1)
  fireEvent.click(controls)
  await waitFor(() => expect(screen.queryByTestId('filament-slot')).toBeNull())
  expect(screen.getAllByText(/\bPLA\b/)).toHaveLength(1)
})
