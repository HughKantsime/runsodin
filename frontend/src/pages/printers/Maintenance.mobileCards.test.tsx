// Mobile web pass, slice 2: Maintenance fleet rows stack under 640 px; desktop markup is unchanged.
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import Maintenance from './Maintenance'
import wire from '../../test-fixtures/certDops-maintenance-status-wire.json'

vi.mock('../../api', async () => {
  const real = await vi.importActual<typeof import('../../api/printers')>('../../api/printers')
  return { maintenance: real.maintenance, fetchAPI: vi.fn().mockResolvedValue({}) }
})
vi.mock('../../permissions', () => ({ canDo: () => true, clearSensitiveBrowserState: vi.fn() }))
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }))

const transport = vi.fn()
const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
function viewport(phone: boolean) {
  vi.stubGlobal('matchMedia', (q: string) => ({ matches: phone && q === '(max-width: 639px)', addEventListener: vi.fn(), removeEventListener: vi.fn() }))
}
function show() {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={c}><MemoryRouter><Maintenance /></MemoryRouter></QueryClientProvider>)
}
beforeEach(() => {
  vi.stubGlobal('fetch', transport)
  transport.mockImplementation(async (url: string) => {
    if (url === '/api/maintenance/status') return json(wire.status)
    if (url.startsWith('/api/maintenance/logs')) return json([])
    if (url === '/api/maintenance/tasks') return json([])
    throw new Error('unexpected ' + url)
  })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('Maintenance fleet rows', () => {
  it('stack under 640 px with a 44 px Log Service button', async () => {
    viewport(true)
    const { container } = show()
    await screen.findByText('118h printed')
    const cards = container.querySelectorAll('[data-layout]')
    expect(cards.length).toBeGreaterThan(0)
    cards.forEach(c => expect(c.getAttribute('data-layout')).toBe('stacked'))
    const header = cards[0].firstElementChild as HTMLElement
    expect(header.className).toContain('flex-col')
    const [button] = screen.getAllByRole('button', { name: 'Log Service' })
    expect(button.className).toContain('min-h-11')
  })

  it('keep the single-line row at 640 px and above', async () => {
    viewport(false)
    const { container } = show()
    await screen.findByText('118h printed')
    container.querySelectorAll('[data-layout]').forEach(c => expect(c.getAttribute('data-layout')).toBe('row'))
    const header = container.querySelector('[data-layout]')!.firstElementChild as HTMLElement
    expect(header.className).toContain('items-center justify-between')
    expect(header.className).not.toContain('flex-col')
  })
})
