import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Floor, { FleetSummary } from './Floor'
import { LoadSpoolModal } from '../inventory/SpoolModals'
import { applyTheme } from '../../hooks/themePreferences'

const data = vi.hoisted(() => ({ printers: [] as any[], maySchedule: true }))
vi.mock('../../permissions', () => ({ canAccessPage: (page: string) => page === 'dashboard' || page === 'jobs' && data.maySchedule, canDo: () => false, getCurrentUser: () => null }))
vi.mock('./data', async importOriginal => ({ ...await importOriginal<typeof import('./data')>(), useNow: () => Date.now(), useFleet: () => ({ data: data.printers }), useFloorData: () => ({ fleet: { data: data.printers }, prints: { data: [] }, queue: { data: [] }, notices: { data: [] } }) }))
vi.mock('../../hooks/useCameraList', () => ({ default: () => ({ data: [], ready: true }) }))
vi.mock('../admin/SystemNoticesBanner', () => ({ default: () => null }))
beforeEach(() => { applyTheme('dark', 'foundry'); localStorage.clear(); data.maySchedule = true; data.printers = [] })
afterEach(() => { cleanup(); applyTheme('dark', 'classic') })
function mount(ui) { return render(<QueryClientProvider client={new QueryClient()}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>) }
it('Floor names its live purpose, density and real scheduling destination', () => {
  mount(<Floor />)
  expect(screen.getByRole('heading', { name: 'Floor' })).toBeInTheDocument()
  expect(screen.getByText('Your printers, live cameras and the next 12 hours.')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Schedule a print' })).toHaveAttribute('href', '/jobs')
  expect(screen.getByRole('group', { name: 'Density' })).toBeInTheDocument()
  expect(screen.getByText('No printers configured.')).toBeInTheDocument()
  expect(screen.queryByText(/Example data/)).toBeNull()
})
it('hides schedule action when Jobs is inaccessible', () => {
  data.maySchedule = false
  mount(<Floor />)
  expect(screen.queryByText('Schedule a print')).toBeNull()
})
it('header derives live fleet totals and renders nothing in Classic', () => {
  const base = { is_active: true, last_seen: new Date().toISOString(), hms_errors: '[]' }
  data.printers = [{ ...base, id: 1, gcode_state: 'RUNNING' }, { ...base, id: 2, gcode_state: 'IDLE' }, { ...base, id: 3, gcode_state: 'PAUSE' }, { ...base, id: 4, gcode_state: 'RUNNING', is_active: false }]
  mount(<FleetSummary />)
  expect(screen.getByText('1 printing · 1 ready')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: '1 needs you' })).toHaveAttribute('href', '/#needs-you')
  cleanup(); applyTheme('dark', 'classic'); const view = mount(<FleetSummary />)
  expect(view.container).toBeEmptyDOMElement()
})
it('spool slot picker uses actual slots including HT and preserves the load payload', () => {
  const onLoad = vi.fn()
  const view = mount(<LoadSpoolModal spool={{ id: 5 }} printers={[{ id: 3, name: 'H2D', api_type: 'bambu', slot_count: 4, filament_slots: [{ slot_number: 1, filament_type: 'empty' }, { slot_number: 17, filament_type: 'SUPPORT' }] }]} onClose={vi.fn()} onLoad={onLoad} />)
  fireEvent.change(screen.getByRole('combobox'), { target: { value: '3' } })
  expect(screen.getAllByRole('radio')).toHaveLength(2)
  fireEvent.click(screen.getByRole('radio', { name: /HT/ }))
  fireEvent.submit(view.baseElement.querySelector('form')!)
  expect(onLoad).toHaveBeenCalledWith({ id: 5, printer_id: 3, slot_number: 17 })
})
