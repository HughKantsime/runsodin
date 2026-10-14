import { cleanup, renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, expect, it, vi } from 'vitest'
import useWebSocket from './useWebSocket'
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
it('retains H2D observations across omitted packets and applies measured zero to the correct printer', async () => {
 let socket: any
 class Socket {
  onmessage: any
  constructor() { socket = this }
  close() {}
 }
 vi.stubGlobal('WebSocket', Socket)
 vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ token: 'synthetic-local-token' }) })))
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
 const other = { id: 200, h2d_nozzles: { nozzle_1: { temp: 55, target: 60 } } }
 client.setQueryData(['printers'], [{ id: 199, h2d_nozzles: { nozzle_1: { temp: 190, target: 200 } } }, other])
 const wrapper = ({ children }: any) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
 renderHook(() => useWebSocket(), { wrapper })
 await waitFor(() => expect(socket?.onmessage).toBeTypeOf('function'))
 const receive = (data: any) => act(() => socket.onmessage({ data: JSON.stringify({ type: 'printer_telemetry', data: { printer_id: 199, ...data } }) }))
 receive({ state: 'IDLE' })
 expect((client.getQueryData(['printers']) as any)[0].h2d_nozzles.nozzle_1).toEqual({ temp: 190, target: 200 })
 receive({ h2d_nozzles: { nozzle_0: { temp: 210, target: 220 }, nozzle_1: { temp: 190, target: 0 } }, external_spools: { left: { material: 'PLA', remain_percent: null }, right: { material: 'PETG', remain_percent: 0 } } })
 const updated: any = client.getQueryData(['printers'])
 expect(updated[0].h2d_nozzles.nozzle_1).toEqual({ temp: 190, target: 0 })
 expect(updated[0].external_spools.left.remain_percent).toBeNull()
 expect(updated[0].external_spools.right.remain_percent).toBe(0)
 expect(updated[1]).toEqual(other)
})
it.each([
 [{ remaining_min: 0 }, { progress_percent: 37, remaining_minutes: 0 }],
 [{ current_layer: 0 }, { progress_percent: 37, remaining_minutes: 125, current_layer: 0 }],
 [{ total_layers: 200 }, { progress_percent: 37, remaining_minutes: 125, total_layers: 200 }],
 [{ progress: 0 }, { progress_percent: 0, remaining_minutes: 125 }],
 [{ speed_level: 4, speed_percent: 166 }, { progress_percent: 37, remaining_minutes: 125 }],
 [{ speed_percent: 0 }, { progress_percent: 37, remaining_minutes: 125 }],
])('applies partial telemetry to the running job without altering other jobs', async (fields, expected) => {
 let socket: any
 class Socket {
  onmessage: any
  constructor() { socket = this }
  close() {}
 }
 vi.stubGlobal('WebSocket', Socket)
 vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ token: 'synthetic-local-token' }) })))
 const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
 const jobs = [{ id: 1, printer_id: 199, status: 'running', progress_percent: 37, remaining_minutes: 125 }, { id: 2, printer_id: 199, status: 'completed', remaining_minutes: 0 }, { id: 3, printer_id: 200, status: 'running', remaining_minutes: 90 }]
 client.setQueryData(['print-jobs'], jobs)
 client.setQueryData(['printers'], [{ id: 199, mc_percent: 37, speed_level: 2, speed_percent: 100 }])
 const wrapper = ({ children }: any) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
 renderHook(() => useWebSocket(), { wrapper })
 await waitFor(() => expect(socket?.onmessage).toBeTypeOf('function'))
 act(() => socket.onmessage({ data: JSON.stringify({ type: 'printer_telemetry', data: { printer_id: 199, ...fields } }) }))
 const updated: any = client.getQueryData(['print-jobs'])
 expect(updated[0]).toMatchObject(expected)
 expect(updated[1]).toEqual(jobs[1])
 expect(updated[2]).toEqual(jobs[2])
 expect(client.getQueryData(['printers'])).toMatchObject([{ id: 199, mc_percent: fields.progress ?? 37, speed_level: fields.speed_level ?? 2, speed_percent: fields.speed_percent ?? 100 }])
})
