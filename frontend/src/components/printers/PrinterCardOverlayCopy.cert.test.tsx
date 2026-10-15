// Cert B: the copy-overlay-URL button carries a camera token for printers with a camera (F02-2.8-02/06).
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import PrinterCard from './PrinterCard'

const state = vi.hoisted(() => ({ create: vi.fn() }))
vi.mock('../../permissions', () => ({ canDo: () => false, getCurrentUser: () => null }))
vi.mock('../../api', async (original) => {
  const actual: any = await original()
  return { ...actual, apiTokens: { ...actual.apiTokens, create: (...a: any[]) => state.create(...a) } }
})
const writes: string[] = []
beforeEach(() => {
  writes.length = 0
  state.create = vi.fn(async () => ({ token: 'odin_synthetic+token' }))
  Object.assign(navigator, { clipboard: { writeText: vi.fn(async (t: string) => { writes.push(t) }) } })
})
afterEach(() => cleanup())

const printer = (over: any = {}) => ({ id: 7, name: 'Bay 7', nickname: null, model: 'X1C', is_active: true, api_type: 'bambu', api_host: '192.0.2.7',
  has_api_key: true, camera_url: 'rtsps://***@192.0.2.7:322/streaming/live/1', camera_enabled: true, filament_slots: [], tags: [], ...over })
const mount = (p: any) => render(<PrinterCard printer={p} allFilaments={[]} spools={[]} onDelete={vi.fn()} onToggleActive={vi.fn()} onUpdateSlot={vi.fn()} onEdit={vi.fn()} onSyncAms={vi.fn()} {...({} as any)} />)

test('printer with a camera: mints a read:cameras token and copies the token-bearing URL', async () => {
  mount(printer())  // no hasCamera prop yet (camera list still loading): printer data decides
  fireEvent.click(screen.getByRole('button', { name: 'Copy OBS overlay URL' }))
  await waitFor(() => expect(writes).toHaveLength(1))
  expect(state.create).toHaveBeenCalledWith({ name: 'OBS overlay: Bay 7 (#7)', scopes: ['read:cameras'] })
  expect(writes[0]).toBe(`${window.location.origin}/overlay/7?token=odin_synthetic%2Btoken`)
})

test('printer without an enabled camera: copies the plain status overlay URL, no token', async () => {
  mount(printer({ camera_enabled: false }))
  fireEvent.click(screen.getByRole('button', { name: 'Copy OBS overlay URL' }))
  await waitFor(() => expect(writes).toHaveLength(1))
  expect(state.create).not.toHaveBeenCalled()
  expect(writes[0]).toBe(`${window.location.origin}/overlay/7`)
})
