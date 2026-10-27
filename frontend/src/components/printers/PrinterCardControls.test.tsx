import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render as rtlRender, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import PrinterCard from './PrinterCard'

const state = vi.hoisted(() => ({ user: { id: 3, username: 'synthetic', role: 'operator', group_id: 1 } as any, allowed: true, lights: vi.fn(), speed: vi.fn() }))
vi.mock('../../permissions', () => ({ canDo: () => state.allowed, getCurrentUser: () => state.user }))
vi.mock('../../api', async (original) => ({ ...await original<any>(), printers: { toggleLights: state.lights, setSpeed: state.speed } }))
const printer = {
 id: 199, org_id: 1, name: 'Synthetic control', nickname: null, model: null, is_active: true,
 api_type: 'bambu', api_host: '192.0.2.1', has_api_key: true, camera_url: null,
 bed_temp: 60, bed_target_temp: 60, nozzle_temp: 210, nozzle_target_temp: 220,
 gcode_state: 'RUNNING', print_stage: 'Printing', lights_on: false,
 filament_slots: [], tags: [], machine_type: null, last_seen: new Date().toISOString(),
}
const props = { allFilaments: [], spools: [], onDelete: vi.fn(), onToggleActive: vi.fn(), onUpdateSlot: vi.fn(), onEdit: vi.fn(), onSyncAms: vi.fn() }
beforeEach(() => {
 state.user = { id: 3, username: 'synthetic', role: 'operator', group_id: 1 }; state.allowed = true
 state.lights.mockReset().mockResolvedValue({ lights_on: true }); state.speed.mockReset().mockResolvedValue({ success: true })
})
afterEach(() => { cleanup(); vi.useRealTimers() })
function selectSpeed(level = 3) {
 fireEvent.change(screen.getByRole('slider', { name: 'Print speed mode' }), { target: { value: String(level) } })
 fireEvent.click(screen.getByRole('button', { name: 'Apply speed' }))
}

it.each(['permission', 'viewer', 'foreign-shared', 'offline', 'expired', 'wrong-protocol', 'no-connection', 'unknown-owner'])('hides mutations for %s while retaining temperature readings', (reason) => {
 let target: any = { ...printer, last_seen: new Date().toISOString() }
 if (reason === 'permission') state.allowed = false
 if (reason === 'viewer') state.user.role = 'viewer'
 if (reason === 'foreign-shared') target = { ...target, org_id: 2, shared: true }
 if (reason === 'offline') target.gcode_state = 'OFFLINE'
 if (reason === 'expired') target.last_seen = new Date(Date.now() - 120000).toISOString()
 if (reason === 'wrong-protocol') target.api_type = 'moonraker'
 if (reason === 'no-connection') target.has_api_key = false
 if (reason === 'unknown-owner') delete target.org_id
 render(<PrinterCard printer={target} {...props} />)
 expect(screen.queryByRole('button', { name: 'Turn lights on' })).not.toBeInTheDocument()
 expect(screen.queryByRole('slider', { name: 'Print speed mode' })).not.toBeInTheDocument()
 expect(screen.getByText(/210°/)).toBeInTheDocument()
 expect(state.lights).not.toHaveBeenCalled(); expect(state.speed).not.toHaveBeenCalled()
})

it.each([{ role: 'operator', group_id: 1, org_id: 1 }, { role: 'admin', group_id: null, org_id: 2 }, { role: 'operator', group_id: 1, org_id: null }])('allows the backend owner/global/unscoped contract %j', async ({ role, group_id, org_id }) => {
 state.user = { ...state.user, role, group_id }
 render(<PrinterCard printer={{ ...printer, org_id, last_seen: new Date().toISOString() }} {...props} />)
 fireEvent.click(screen.getByRole('button', { name: 'Turn lights on' }))
 await waitFor(() => expect(state.lights).toHaveBeenCalledExactlyOnceWith(199))
 expect(await screen.findByRole('status')).toHaveTextContent('Command accepted; printer confirmation pending')
})

it('sends speed once and preserves physical confirmation as pending', async () => {
 render(<PrinterCard printer={{ ...printer, last_seen: new Date().toISOString() }} {...props} />)
 selectSpeed()
 expect(state.speed).toHaveBeenCalledExactlyOnceWith(199, 3)
 expect(await screen.findByRole('status')).toHaveTextContent('Command accepted; printer confirmation pending')
})

it.each(['lights', 'speed'])('shows unconfirmed %s receipt without claiming execution', async (action) => {
 state[action].mockResolvedValue(undefined)
 render(<PrinterCard printer={{ ...printer, last_seen: new Date().toISOString() }} {...props} />)
 if (action === 'lights') fireEvent.click(screen.getByRole('button', { name: 'Turn lights on' })); else selectSpeed()
 expect(await screen.findByRole('status')).toHaveTextContent('Command not confirmed')
})

it.each(['lights', 'speed'])('catches failed %s responses without exposing their contents', async (action) => {
 state[action].mockRejectedValue(new Error('synthetic-private-marker'))
 render(<PrinterCard printer={{ ...printer, last_seen: new Date().toISOString() }} {...props} />)
 if (action === 'lights') fireEvent.click(screen.getByRole('button', { name: 'Turn lights on' })); else selectSpeed()
 expect(await screen.findByRole('status')).toHaveTextContent('Command not confirmed')
 expect(screen.queryByText(/synthetic-private-marker/)).not.toBeInTheDocument()
})

it.each(['permission', 'role', 'heartbeat'])('rechecks %s at click even without rerender', (reason) => {
 vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-11T12:00:00Z'))
 render(<PrinterCard printer={{ ...printer, last_seen: '2026-10-11T11:59:59Z' }} {...props} />)
 fireEvent.change(screen.getByRole('slider', { name: 'Print speed mode' }), { target: { value: '3' } })
 const button = screen.getByRole('button', { name: 'Apply speed' })
 if (reason === 'permission') state.allowed = false
 if (reason === 'role') state.user.role = 'viewer'
 if (reason === 'heartbeat') vi.setSystemTime(new Date('2026-10-11T12:02:00Z'))
 fireEvent.click(button)
 expect(state.speed).not.toHaveBeenCalled()
})

it('prevents duplicate or overlapping controls while a command is pending', async () => {
 let resolve: any
 state.lights.mockImplementation(() => new Promise(r => { resolve = r }))
 render(<PrinterCard printer={{ ...printer, last_seen: new Date().toISOString() }} {...props} />)
 const light = screen.getByRole('button', { name: 'Turn lights on' })
 fireEvent.click(light); fireEvent.click(light); selectSpeed()
 expect(state.lights).toHaveBeenCalledTimes(1); expect(state.speed).not.toHaveBeenCalled()
 await act(async () => resolve({ lights_on: true }))
 expect(screen.getByRole('status')).toHaveTextContent('Command accepted')
})

it.each(['unmount', 'principal', 'printer'])('ignores command completion after %s changes', async (change) => {
 let resolve: any
 state.speed.mockImplementation(() => new Promise(r => { resolve = r }))
 const view = render(<PrinterCard printer={{ ...printer, last_seen: new Date().toISOString() }} {...props} />)
 selectSpeed()
 if (change === 'unmount') view.unmount()
 if (change === 'principal') { state.user = { ...state.user, id: 4 }; view.rerender(<PrinterCard printer={printer} {...props} />) }
 if (change === 'printer') view.rerender(<PrinterCard printer={{ ...printer, id: 200 }} {...props} />)
 await act(async () => resolve({ success: true }))
 expect(screen.queryByText(/Command accepted/)).not.toBeInTheDocument()
})

it('keeps idle light control available while hiding idle speed control', () => {
 render(<PrinterCard printer={{ ...printer, gcode_state: 'IDLE', last_seen: new Date().toISOString() }} {...props} />)
 expect(screen.getByRole('button', { name: 'Turn lights on' })).toBeInTheDocument()
 expect(screen.queryByRole('slider', { name: 'Print speed mode' })).not.toBeInTheDocument()
})
it('provides a discrete speed slider and shows only the printer-reported percentage', () => {
 render(<PrinterCard printer={{ ...printer, speed_level: 2, speed_percent: 100, last_seen: new Date().toISOString() }} {...props} />)
 expect(screen.getByRole('slider', { name: 'Print speed mode' })).toHaveValue('2')
 expect(screen.getByText('Last reported speed: 100%')).toBeInTheDocument()
})
it.each([1,2,3,4])('applies protocol mode %s only after explicit Apply, without changing reported telemetry', async level => {
 render(<PrinterCard printer={{ ...printer, speed_level: 2, speed_percent: 100, last_seen: new Date().toISOString() }} {...props} />)
 fireEvent.change(screen.getByRole('slider', { name: 'Print speed mode' }), { target: { value: String(level) } })
 expect(state.speed).not.toHaveBeenCalled()
 fireEvent.click(screen.getByRole('button', { name: 'Apply speed' }))
 expect(state.speed).toHaveBeenCalledExactlyOnceWith(199, level)
 expect(await screen.findByRole('status')).toHaveTextContent('Command accepted; printer confirmation pending')
 expect(screen.getByText('Last reported speed: 100%')).toBeInTheDocument()
 expect(screen.getByText('Last reported mode: Standard')).toBeInTheDocument()
})
it('keeps absent observations unknown and requires explicit Apply for its selected default', async () => {
 render(<PrinterCard printer={{ ...printer, last_seen: new Date().toISOString() }} {...props} />)
 expect(screen.getByText('Reported speed unknown')).toBeInTheDocument()
 expect(screen.getByText('Printer speed mode unknown')).toBeInTheDocument()
 expect(state.speed).not.toHaveBeenCalled()
 fireEvent.click(screen.getByRole('button', { name: 'Apply speed' }))
 expect(state.speed).toHaveBeenCalledExactlyOnceWith(199, 2)
 expect(await screen.findByRole('status')).toHaveTextContent('Command accepted')
 expect(screen.getByText('Reported speed unknown')).toBeInTheDocument()
})
it('shows measured zero and read-only reported speed to a viewer', () => {
 state.user.role = 'viewer'
 render(<PrinterCard printer={{ ...printer, speed_level: 1, speed_percent: 0, last_seen: new Date().toISOString() }} {...props} />)
 expect(screen.getByText('Last reported speed: 0%')).toBeInTheDocument()
 expect(screen.getByText('Last reported mode: Silent')).toBeInTheDocument()
 expect(screen.queryByRole('slider')).not.toBeInTheDocument()
})
it.each(['principal','printer'])('does not reuse a selected draft after %s changes', change => {
 const view = render(<PrinterCard printer={{ ...printer, last_seen: new Date().toISOString() }} {...props} />)
 fireEvent.change(screen.getByRole('slider'), { target: { value: '4' } })
 if (change === 'principal') state.user = { ...state.user, id: 4 }
 view.rerender(<PrinterCard printer={{ ...printer, id: change === 'printer' ? 200 : 199, last_seen: new Date().toISOString() }} {...props} />)
 expect(screen.getByRole('slider')).toHaveValue('2')
 expect(state.speed).not.toHaveBeenCalled()
})

function render(ui: Parameters<typeof rtlRender>[0]) {
 const client = new QueryClient({defaultOptions:{queries:{retry:false}}})
 return rtlRender(ui, {wrapper:({children})=><QueryClientProvider client={client}>{children}</QueryClientProvider>})
}
vi.mock('../../api/printerMaintenance',()=>({printerMaintenance:{get:vi.fn().mockResolvedValue({state:'ok',note:'',set_by:null,set_by_name:null,set_at:null})}}))
