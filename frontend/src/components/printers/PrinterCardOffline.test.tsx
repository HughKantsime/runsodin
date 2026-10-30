import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render as rtlRender, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import PrinterCard from './PrinterCard'
import { ActiveJobPanel } from './PrinterPanels'
vi.mock('../../permissions', () => ({ canDo: () => false, getCurrentUser: () => null }))
beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-11T12:00:00Z')) })
afterEach(() => { cleanup(); vi.useRealTimers() })
const printer = {
 id: 199, name: 'Synthetic offline printer', nickname: null, model: null,
 is_active: true, api_type: null, api_host: null, has_api_key: false,
 camera_url: null, bed_temp: null, bed_target_temp: null, nozzle_temp: null,
 nozzle_target_temp: null, gcode_state: 'OFFLINE', print_stage: 'Offline',
 lights_on: null, filament_slots: [], tags: [], machine_type: null,
 last_seen: '2026-10-11T11:59:59Z',
}
const props = { allFilaments: [], spools: [], onDelete: vi.fn(), onToggleActive: vi.fn(), onUpdateSlot: vi.fn(), onEdit: vi.fn(), onSyncAms: vi.fn() }
it('shows persisted offline immediately and restores after a persisted valid report', () => {
 const view = render(<PrinterCard printer={printer} {...props} />)
 expect(screen.getByTestId('printer-status')).toHaveTextContent('Offline')
 view.rerender(<PrinterCard printer={{ ...printer, gcode_state: 'IDLE', print_stage: 'Idle', last_seen: '2026-10-11T12:00:00Z' }} {...props} />)
 expect(screen.getByTestId('printer-status')).toHaveTextContent('Online')
})
it('expires a silent printer at the heartbeat boundary regardless of retained print state', () => {
 render(<PrinterCard printer={{ ...printer, gcode_state: 'RUNNING', last_seen: '2026-10-11T11:58:30Z' }} {...props} />)
 expect(screen.getByTestId('printer-status')).toHaveTextContent('Offline')
})
it('shows active print progress and remaining time on the printer card', () => {
 render(<PrinterCard printer={{ ...printer, gcode_state: 'RUNNING', mc_percent: 37, mc_remaining_time: 125 }} {...props} />)
 expect(screen.getByRole('progressbar', { name: 'Print progress' })).toHaveAttribute('aria-valuenow', '37')
 expect(screen.getByText('2h 5m left')).toBeInTheDocument()
})
it('does not present stale progress as an active print while offline', () => {
 render(<PrinterCard printer={{ ...printer, mc_percent: 37, mc_remaining_time: 125 }} {...props} />)
 expect(screen.queryByRole('progressbar', { name: 'Print progress' })).not.toBeInTheDocument()
 expect(screen.queryByText('2h 5m left')).not.toBeInTheDocument()
})
it.each([
 { display: 'Class project', file: 'printer-file.3mf', expected: 'Class project' },
 { display: null, file: 'printer-file.3mf', expected: 'printer-file.3mf' },
 { display: '   ', file: 'printer-file.3mf', expected: 'printer-file.3mf' },
 { display: null, file: null, expected: 'Unnamed print' },
 { display: null, file: 'odin-dispatch-abcd-1234.gcode', expected: 'Unnamed print' },
])('resolves the active print name from display name and printer file: $expected', ({ display, file, expected }) => {
 render(<PrinterCard printer={{ ...printer, gcode_state: 'RUNNING', current_print_display_name: display, gcode_file: file }} {...props} />)
 expect(screen.getByText(expected)).toBeInTheDocument()
})
it('shares measured progress with the camera current-job panel and hides it when heartbeat expires', () => {
 const active = { ...printer, gcode_state: 'PAUSE', mc_percent: 0, mc_remaining_time: 59.6 }
 const view = render(<ActiveJobPanel printer={active} />)
 expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '0')
 expect(screen.getByText('1h 0m left')).toBeInTheDocument()
 view.rerender(<ActiveJobPanel printer={{ ...active, last_seen: '2026-10-11T11:58:30Z' }} />)
 expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
 expect(screen.queryByText('Current Job')).not.toBeInTheDocument()
})
it('does not invent progress when active printer telemetry is absent', () => {
 render(<PrinterCard printer={{ ...printer, gcode_state: 'RUNNING' }} {...props} />)
 expect(screen.getByText('Progress unknown')).toBeInTheDocument()
 expect(screen.getByText('Time remaining unknown')).toBeInTheDocument()
 expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
})
it('renders H2D badge, dual nozzle temperatures and external-spool zero measurements', () => {
 render(<PrinterCard printer={{ ...printer, machine_type: 'H2D', gcode_state: 'IDLE', nozzle_temp: 210, nozzle_target_temp: 220, h2d_nozzles: { nozzle_1: { temp: 190, target: 200 } }, external_spools: { left: { color: '#ff0000', material: 'PLA', remain_percent: 0 }, right: { color: '#00ff00', material: 'PETG', remain_percent: 75 } } }} {...props} />)
 expect(screen.getByText('H2D')).toBeInTheDocument()
 expect(screen.getByTitle('Left Nozzle')).toHaveTextContent('L 210°/220°')
 expect(screen.getByTitle('Right Nozzle')).toHaveTextContent('R 190°/200°')
 expect(screen.getByText('Ext-L')).toBeInTheDocument()
 expect(screen.getByText('Ext-R')).toBeInTheDocument()
 expect(screen.getByText('0%')).toBeInTheDocument()
 expect(screen.getByText('75%')).toBeInTheDocument()
})
it('does not expose light controls when printer editing is denied', () => {
 render(<PrinterCard printer={{ ...printer, api_type: 'bambu', gcode_state: 'IDLE', lights_on: false }} {...props} />)
 expect(screen.queryByRole('button', { name: 'Turn lights on' })).not.toBeInTheDocument()
})
it('does not expose active printer speed controls when printer editing is denied', () => {
 render(<PrinterCard printer={{ ...printer, api_type: 'bambu', gcode_state: 'RUNNING' }} {...props} />)
 expect(screen.queryByTitle('Silent')).not.toBeInTheDocument()
 expect(screen.queryByRole('slider', { name: 'Print speed mode' })).not.toBeInTheDocument()
})

function render(ui: Parameters<typeof rtlRender>[0]) {
 const client = new QueryClient({defaultOptions:{queries:{retry:false}}})
 return rtlRender(ui, {wrapper:({children})=><QueryClientProvider client={client}>{children}</QueryClientProvider>})
}
vi.mock('../../api/printerMaintenance',()=>({printerMaintenance:{get:vi.fn().mockResolvedValue({state:'ok',note:'',set_by:null,set_by_name:null,set_at:null})}}))
