import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Printers from './Printers'
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, atPrinterLimit: () => false }) }))
vi.mock('../../permissions', () => ({ canDo: () => false, getCurrentUser: () => null }))
beforeEach(() => { sessionStorage.clear(); localStorage.clear() })
afterEach(cleanup)
const base = { nickname: null, model: 'Synthetic model', is_active: true, last_seen: new Date().toISOString(), gcode_state: 'IDLE', print_stage: 'Idle', filament_slots: [], tags: [] }
function page(activeState = 'RUNNING') {
 const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
 client.setQueryData(['printers', null], [
  { ...base, id: 1, name: 'Zulu', nickname: 'Workshop', api_type: 'bambu', tags: ['business'] },
  { ...base, id: 2, name: 'Alpha', api_type: 'moonraker', tags: ['home'], gcode_state: activeState, print_stage: 'Heating' },
  { ...base, id: 3, name: 'Offline', api_type: 'bambu', gcode_state: 'OFFLINE', print_stage: 'Printing' },
 ])
 return render(<QueryClientProvider client={client}><MemoryRouter><Printers /></MemoryRouter></QueryClientProvider>)
}
const names = () => screen.queryAllByTestId('printer-card').map(card => card.getAttribute('data-printer-name'))
it('searches nicknames without case sensitivity, combines type/tag filters and sorts names', () => {
 page()
 fireEvent.change(screen.getByPlaceholderText('Search printers...'), { target: { value: 'WORK' } })
 expect(names()).toEqual(['Zulu'])
 fireEvent.change(screen.getByPlaceholderText('Search printers...'), { target: { value: '' } })
 fireEvent.change(screen.getByLabelText('Filter printers by type'), { target: { value: 'bambu' } })
 fireEvent.change(screen.getByLabelText('Filter printers by tag'), { target: { value: 'business' } })
 expect(names()).toEqual(['Zulu'])
 fireEvent.change(screen.getByLabelText('Filter printers by type'), { target: { value: 'all' } })
 fireEvent.change(screen.getByLabelText('Filter printers by tag'), { target: { value: '' } })
 fireEvent.change(screen.getByLabelText('Sort printers'), { target: { value: 'name_asc' } })
 expect(names()).toEqual(['Alpha', 'Offline', 'Zulu'])
})
it.each(['RUNNING', 'PAUSE'])('printing filter uses current online %s state rather than stale or incidental stages', state => {
 page(state)
 fireEvent.change(screen.getByLabelText('Filter printers by status'), { target: { value: 'printing' } })
 expect(names()).toEqual(['Alpha'])
})
it('preserves card-size preference and filter choices across remount', () => {
 const view = page()
 fireEvent.click(screen.getByRole('button', { name: 'XL', exact: true }))
 expect(localStorage.getItem('printerCardSize')).toBe('XL')
 fireEvent.change(screen.getByPlaceholderText('Search printers...'), { target: { value: 'Alpha' } })
 expect(sessionStorage.getItem('printers_search')).toBe('Alpha')
 view.unmount()
 page()
 expect(screen.getByPlaceholderText('Search printers...')).toHaveValue('Alpha')
 expect(names()).toEqual(['Alpha'])
 expect(screen.getByRole('button', { name: 'XL', exact: true }).className).toContain('bg-[var(--brand-primary)]')
})
