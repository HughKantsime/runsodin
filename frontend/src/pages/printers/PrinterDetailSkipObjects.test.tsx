import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import PrinterDetail from './PrinterDetail'
import { printers as objectApi } from '../../api/printers'

const state = vi.hoisted(() => ({ role: 'operator', permitted: true, type: 'bambu', gcode: 'RUNNING', online: true, group_id: 1 as number | null }))
vi.mock('../../permissions', () => ({ canDo: () => state.permitted, canAccessPage: () => false, getCurrentUser: () => ({ role: state.role, group_id: state.group_id }) }))
vi.mock('../../components/printers/MaintenanceBanner', () => ({ default: () => null }))
vi.mock('../../components/printers/DispatchRecoveryPanel', () => ({ default: () => null }))
vi.mock('../../api/client', () => ({ fetchAPI: vi.fn().mockResolvedValue([]) }))
vi.mock('../../api/printers', () => ({ printers: { printObjects: vi.fn(), skipObjects: vi.fn() } }))
vi.mock('../../api', () => ({
  printers: { get: vi.fn(async () => ({ id: 9, name: 'Printer detail', api_type: state.type, gcode_state: state.gcode, current_job: 'Parts',
    last_seen: state.online ? new Date().toISOString() : null, experimental_actions_enabled: false })) },
  printerTelemetry: { get: vi.fn().mockResolvedValue([]) }, printJobs: { list: vi.fn().mockResolvedValue([]) }, jobs: { list: vi.fn().mockResolvedValue([]) },
}))
beforeEach(() => {
  Object.assign(state, { role: 'operator', permitted: true, type: 'bambu', gcode: 'RUNNING', online: true, group_id: 1 })
  vi.mocked(objectApi.printObjects).mockResolvedValue({
    printer_id: 9, supported: true, can_skip: true, experimental: false, source: 'bambu_3mf',
    reason: null, reason_code: null, retry_after_seconds: null, skipped_reported: true, current_object_id: null,
    print: { print_job_id: 912, job_id: 501, name: 'Parts', plate: 1 }, max_selectable: 1,
    picture: { available: false, aspect: null, front_edge: null },
    objects: ['15', '79'].map((id, i) => ({ id, number: i + 1, name: 'Bracket', state: 'printing', skippable: true, bounds: null, outline: null })),
  })
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); document.documentElement.removeAttribute('data-theme-family') })
function mount(hash = '') {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/printers/9' + hash]}>
    <Routes><Route path="/printers/:id" element={<PrinterDetail />} /></Routes>
  </MemoryRouter></QueryClientProvider>)
}

it.each([
  ['classic', false], ['classic', true], ['foundry', false], ['foundry', true],
])('offers Bambu skip without opt-in and loads only on opening (%s, phone=%s)', async (family, phone) => {
  document.documentElement.dataset.themeFamily = family as string
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: phone && query.includes('max-width'), media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
  mount()
  const button = await screen.findByRole('button', { name: 'Skip objects' })
  expect(button.closest('details')).toBeNull()
  expect(button).toHaveClass('bg-transparent', 'border')
  expect(objectApi.printObjects).not.toHaveBeenCalled()
  fireEvent.click(button)
  expect(await screen.findByText('Parts · Plate 1')).toBeInTheDocument()
  expect(objectApi.printObjects).toHaveBeenCalledExactlyOnceWith(9)
  expect(screen.queryByRole('link', { name: 'Skip objects settings' })).not.toBeInTheDocument()
  expect(document.getElementById('experimental-opt-in-9')).not.toBeInTheDocument()
  expect(screen.getByRole('dialog').querySelector('[data-foundry-ui]') !== null).toBe(family === 'foundry')
  expect(objectApi.skipObjects).not.toHaveBeenCalled()
})

it.each([
  { role: 'viewer' }, { role: 'student' }, { permitted: false }, { online: false },
  { type: 'moonraker' }, { type: 'elegoo' }, { gcode: 'IDLE' }, { gcode: 'FINISH' }, { gcode: 'OFFLINE' },
])('hides the action for %j', async change => {
  Object.assign(state, change)
  mount()
  await screen.findByRole('heading', { name: 'Printer detail', level: 1 })
  expect(screen.queryByRole('button', { name: 'Skip objects' })).not.toBeInTheDocument()
  expect(objectApi.printObjects).not.toHaveBeenCalled()
})

it.each(['RUNNING', 'PRINTING', 'PAUSE', 'PAUSED'])('offers the action for an admin in %s', async gcode => {
  state.role = 'admin'; state.gcode = gcode
  mount()
  expect(await screen.findByRole('button', { name: 'Skip objects' })).toBeInTheDocument()
})


it.each(['classic', 'foundry'])('hides the Bambu Experimental switch for a platform admin in %s', async family => {
  document.documentElement.dataset.themeFamily = family
  state.role = 'admin'; state.group_id = null
  mount('#experimental-opt-in-9')
  expect(await screen.findByRole('button', { name: 'Skip objects' })).toBeInTheDocument()
  expect(screen.queryByText('Experimental features')).not.toBeInTheDocument()
  expect(screen.queryByRole('checkbox', { name: /skipping objects/i })).not.toBeInTheDocument()
})

it('opens Elegoo Experimental features from the hash for a non-superadmin', async () => {
  state.type = 'elegoo'
  mount('#experimental-opt-in-9')
  const disclosure = (await screen.findByText('Experimental features')).closest('details')!
  expect(disclosure).toHaveAttribute('open')
  expect(screen.getByText('Only a platform administrator can change this.')).toBeInTheDocument()
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
})
