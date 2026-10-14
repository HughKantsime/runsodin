import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import Printers from './Printers'
import useWebSocket from '../../hooks/useWebSocket'
vi.mock('../../LicenseContext', () => ({ useLicense: () => ({ isPro: true, atPrinterLimit: () => false }) }))
vi.mock('../../permissions', () => ({ canDo: () => false, getCurrentUser: () => ({ id: 3, role: 'viewer', group_id: 1 }) }))
vi.mock('../../contexts/OrgContext', () => ({ useOrg: () => ({ orgId: 1 }) }))
afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear() })

it('updates the actual organization-scoped printer page from live telemetry without refetch', async () => {
 let socket: any
 class Socket { onmessage: any; constructor() { socket = this }; close() {} }
 vi.stubGlobal('WebSocket', Socket)
 vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ token: 'synthetic-local-token' }) })))
 const client = new QueryClient({ defaultOptions: { queries: { enabled: false, retry: false } } })
 const base = { id: 199, org_id: 1, name: 'Synthetic live speed', nickname: null, model: null, is_active: true, api_type: 'bambu', api_host: '192.0.2.1', has_api_key: true, last_seen: new Date().toISOString(), gcode_state: 'RUNNING', print_stage: 'Printing', filament_slots: [], tags: [], speed_level: 2, speed_percent: 100 }
 client.setQueryData(['printers', 1], [base])
 client.setQueryData(['printers', 2], [{ ...base, id: 200, org_id: 2, name: 'Other scoped printer' }])
 client.setQueryData(['printers'], [base])
 client.setQueryData(['printers', 'non-list'], { count: 7 })
 function Page() { useWebSocket(); return <Printers /> }
 render(<QueryClientProvider client={client}><MemoryRouter><Page /></MemoryRouter></QueryClientProvider>)
 expect(screen.getByText('Last reported speed: 100%')).toBeInTheDocument()
 await waitFor(() => expect(socket?.onmessage).toBeTypeOf('function'))
 act(() => socket.onmessage({ data: JSON.stringify({ type: 'printer_telemetry', data: { printer_id: 199, speed_level: 3, speed_percent: 124 } }) }))
 await waitFor(() => expect(screen.getByText('Last reported speed: 124%')).toBeInTheDocument())
 expect(screen.getByText('Last reported mode: Sport')).toBeInTheDocument()
 expect((client.getQueryData(['printers', 2]) as any)[0].speed_percent).toBe(100)
 expect(client.getQueryData(['printers', 'non-list'])).toEqual({ count: 7 })
 expect(screen.queryByRole('slider')).not.toBeInTheDocument()
})
