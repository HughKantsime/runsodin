import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import PrinterCard from './PrinterCard'
const api = vi.hoisted(() => ({ ams: vi.fn(), telemetry: vi.fn(), nozzle: vi.fn(), hms: vi.fn() }))
vi.mock('../../permissions', () => ({ canDo: () => false, getCurrentUser: () => ({ id: 4, role: 'viewer', group_id: 1 }) }))
vi.mock('../../api', async original => ({ ...await original<any>(), getAmsEnvironment: api.ams, printerTelemetry: { get: api.telemetry, nozzle: api.nozzle, hmsHistory: api.hms } }))
const base: any = { id: 199, org_id: 1, name: 'Synthetic panel printer', is_active: true, api_type: 'bambu', filament_slots: [], tags: [], gcode_state: 'IDLE', last_seen: new Date().toISOString() }
const props = { allFilaments: [], spools: [], onDelete: vi.fn(), onToggleActive: vi.fn(), onUpdateSlot: vi.fn(), onEdit: vi.fn(), onSyncAms: vi.fn() }
beforeEach(() => {
 for (const fn of Object.values(api)) fn.mockReset().mockImplementation(() => new Promise(() => {}))
})
afterEach(cleanup)
it.each([
 ['AMS environment data', 'AMS Environment', 'ams'],
 ['Print telemetry', 'Print Telemetry', 'telemetry'],
 ['Nozzle lifecycle', 'Nozzle Lifecycle', 'nozzle'],
 ['HMS error history', 'HMS Error History', 'hms'],
] as const)('opens and toggles %s inside its own card with the correct target', async (button, heading, method) => {
 const view = render(<PrinterCard printer={base} {...props} />)
 fireEvent.click(screen.getByRole('button', { name: button }))
 expect(await within(view.container).findByRole('heading', { name: heading })).toBeInTheDocument()
 expect(api[method].mock.calls[0][0]).toBe(199)
 expect(screen.getByRole('button', { name: button })).toHaveAttribute('aria-pressed', 'true')
 fireEvent.click(screen.getByRole('button', { name: button }))
 expect(screen.queryByRole('heading', { name: heading })).not.toBeInTheDocument()
 expect(screen.getByRole('button', { name: button })).toHaveAttribute('aria-pressed', 'false')
})
it('switches panel without retaining the previous panel or opening a sibling card', async () => {
 const view = render(<div><section data-testid="first"><PrinterCard printer={base} {...props} /></section><section data-testid="second"><PrinterCard printer={{ ...base, id: 200 }} {...props} /></section></div>)
 const first = within(view.getByTestId('first')), second = within(view.getByTestId('second'))
 fireEvent.click(first.getByRole('button', { name: 'Nozzle lifecycle' }))
 expect(await first.findByRole('heading', { name: 'Nozzle Lifecycle' })).toBeInTheDocument()
 expect(second.queryByRole('heading', { name: 'Nozzle Lifecycle' })).not.toBeInTheDocument()
 fireEvent.click(first.getByRole('button', { name: 'HMS error history' }))
 expect(await first.findByRole('heading', { name: 'HMS Error History' })).toBeInTheDocument()
 expect(first.queryByRole('heading', { name: 'Nozzle Lifecycle' })).not.toBeInTheDocument()
 expect(second.queryByRole('heading', { name: 'HMS Error History' })).not.toBeInTheDocument()
})
