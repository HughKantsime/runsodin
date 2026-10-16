// Cert-E integ F14 14.3: the real useWebSocket hook consumes the producer wire fixture
// captured from the real create_app() /ws endpoint (tests/test_contracts/test_cert_E_integ_ws.py).
import { cleanup, renderHook, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import useWebSocket from './useWebSocket'
import wire from '../test-fixtures/cert-E-integ-ws-telemetry.json'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function mount() {
  const sockets: any[] = []
  class Socket {
    url: string
    onmessage: any
    sent: any[] = []
    constructor(url: string) { this.url = url; sockets.push(this) }
    send(v: any) { this.sent.push(v) }
    close() {}
  }
  const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({ token: 'synthetic-ws-token' }) }))
  vi.stubGlobal('WebSocket', Socket)
  vi.stubGlobal('fetch', fetchMock)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  client.setQueryData(['printers'], structuredClone(wire.printers_list))
  client.setQueryData(['printer', '9001'], structuredClone(wire.printer_detail))
  const wrapper = ({ children }: any) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  renderHook(() => useWebSocket(), { wrapper })
  return { client, sockets, fetchMock }
}

describe('CertEInteg WebSocket live push', () => {
  it('connects to /ws with a ws-token and patches the printers cache from the real telemetry frame', async () => {
    const { client, sockets, fetchMock } = mount()
    await waitFor(() => expect(sockets[0]?.onmessage).toBeTypeOf('function'))
    expect(fetchMock).toHaveBeenCalledWith('/api/auth/ws-token', expect.objectContaining({ method: 'POST' }))
    expect(new URL(sockets[0].url).pathname).toBe('/ws')
    expect(new URL(sockets[0].url).searchParams.get('token')).toBe('synthetic-ws-token')
    const frame = wire.frames[0]
    act(() => sockets[0].onmessage({ data: JSON.stringify(frame) }))
    const printer: any = (client.getQueryData(['printers']) as any[]).find(p => p.id === 9001)
    expect(printer.nozzle_temp).toBe(frame.data.nozzle_temp)
    expect(printer.bed_temp).toBe(frame.data.bed_temp)
    expect(printer.gcode_state).toBe(frame.data.state)
    expect(printer.mc_percent).toBe(frame.data.progress)
  })

  it('does not patch the printer-detail cache, so the detail page still depends on its 5 s poll', async () => {
    const { client, sockets } = mount()
    await waitFor(() => expect(sockets[0]?.onmessage).toBeTypeOf('function'))
    act(() => sockets[0].onmessage({ data: JSON.stringify(wire.frames[0]) }))
    // PrinterDetail.tsx reads ['printer', id] with refetchInterval: 5000; WS leaves it stale.
    expect(client.getQueryData(['printer', '9001'])).toEqual(wire.printer_detail)
  })
})
