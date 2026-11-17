import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
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
  vi.mocked(objectApi.printObjects).mockResolvedValue({ supported: false, reason_code: 'experimental_not_enabled', reason: 'Enable Experimental actions first.', objects: [], can_skip: false } as any)
})
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); document.documentElement.removeAttribute('data-theme-family') })
function mount(hash = '') {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter initialEntries={['/printers/9' + hash]}>
    <Routes><Route path="/printers/:id" element={<PrinterDetail />} /></Routes>
  </MemoryRouter></QueryClientProvider>)
}

it.each([
  ['classic', false], ['classic', true], ['foundry', false], ['foundry', true],
])('offers the action with opt-in off, loads only on opening (%s, phone=%s)', async (family, phone) => {
  document.documentElement.dataset.themeFamily = family as string
  vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: phone && query.includes('max-width'), media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
  mount()
  const button = await screen.findByRole('button', { name: 'Skip objects' })
  expect(button.closest('details')).toBeNull()
  expect(button).toHaveClass('bg-transparent', 'border')
  expect(objectApi.printObjects).not.toHaveBeenCalled()
  fireEvent.click(button)
  expect(await screen.findByText('Enable Experimental actions first.')).toBeInTheDocument()
  expect(objectApi.printObjects).toHaveBeenCalledExactlyOnceWith(9)
  expect(screen.getByRole('link', { name: 'Skip objects settings' })).toHaveAttribute('href', '#experimental-opt-in-9')
  expect(document.getElementById('experimental-opt-in-9')).toBeInTheDocument()
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


it.each(['classic', 'foundry'])('opens the settings disclosure and focuses its checkbox in %s', async family => {
  document.documentElement.dataset.themeFamily = family
  state.role = 'admin'; state.group_id = null
  Element.prototype.scrollIntoView = vi.fn()
  mount()
  const button = await screen.findByRole('button', { name: 'Skip objects' })
  const disclosure = screen.getByText('Experimental features').closest('details')!
  expect(disclosure).not.toHaveAttribute('open')
  fireEvent.click(button)
  fireEvent.click(await screen.findByRole('link', { name: 'Skip objects settings' }))
  await waitFor(() => expect(disclosure).toHaveAttribute('open'))
  expect(screen.getByRole('checkbox', { name: 'Allow skipping objects (Experimental)' })).toHaveFocus()
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})

it.each(['bambu', 'elegoo'])('opens %s Experimental features from the hash for a non-superadmin', async type => {
  state.type = type
  mount('#experimental-opt-in-9')
  const disclosure = (await screen.findByText('Experimental features')).closest('details')!
  expect(disclosure).toHaveAttribute('open')
  expect(screen.getByText('Only a platform administrator can change this.')).toBeInTheDocument()
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
})
