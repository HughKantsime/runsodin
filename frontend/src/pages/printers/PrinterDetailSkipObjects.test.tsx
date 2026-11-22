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
  filaments: { combined: vi.fn().mockResolvedValue([]) }, spools: { list: vi.fn().mockResolvedValue([]) },
  printers: { updateSlot: vi.fn().mockResolvedValue({}), get: vi.fn(async () => ({ id: 9, name: 'Printer detail', api_type: state.type, gcode_state: state.gcode, current_job: 'Parts',
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

it.each(['classic', 'foundry'])('shows the read-only H3C rack once on the %s printer page', async family => {
  const { printers } = await import('../../api')
  const capture = await import('../../../../tests/fixtures/telemetry/h3c-one-filament-2026-11-21.json')
  const nozzles = capture.message.print.device.nozzle.info.map(row => ({
    nozzle_id: row.id, position: row.addr_id + 1, side: 'rack', diameter: row.diameter, type: row.type,
    virtual_slot_id: row.snow >> 8, mounted: row.id === 20,
    present: true, material: null, flow: null, wear: row.wear, observed_at: '2026-11-21T19:40:00Z',
  }))
  const slots = nozzles.map(nozzle => {
    const tray = capture.message.print.vir_slot.find(slot => Number(slot.id) === nozzle.virtual_slot_id)!
    return { slot_number: nozzle.position, filament_type: 'PETG', color: 'Slate', color_hex: '112233', remaining: 80,
      rack_filament: { id: Number(tray.id), type: tray.tray_type, preset: tray.tray_info_idx, color: tray.tray_color, observed_at: '2026-11-21T19:57:00Z' } }
  })
  vi.mocked(printers.get).mockResolvedValueOnce({
    id: 9, name: 'Printer detail', api_type: 'bambu', model: 'H3C', gcode_state: 'RUNNING',
    last_seen: new Date().toISOString(), installed_nozzles: nozzles, filament_slots: slots,
  } as Awaited<ReturnType<typeof printers.get>>)
  document.documentElement.dataset.themeFamily = family
  mount()
  const rack = await screen.findByRole('region', { name: 'Nozzle rack' })
  const rows = rack.querySelectorAll('li')
  expect(rows).toHaveLength(9)
  expect(rows[0]).toHaveTextContent('Position 1 · Standard (1.75 mm) (HS01) · 0.4 mm')
  expect(screen.getAllByText(/Hand-fed at the tool/)).toHaveLength(1)
  expect(rack).toHaveTextContent('Position 8 · TPU 2.85 mm (HT01) · 0.4 mm')
  expect(rows[0]).toHaveTextContent('Hand-fed at the tool: PLA, GFA01, #AE96D4')
  const editors = screen.getAllByTestId('filament-slot')
  expect(editors).toHaveLength(9)
  expect(screen.getAllByText(/\bPETG\b/)).toHaveLength(9)
  editors.forEach((editor, index) => {
    expect(editor).toHaveTextContent(`Position ${index + 1}`)
    expect(rows[index]).toContainElement(editor)
    expect(editor).toHaveTextContent('PETG')
    if (family === 'classic') expect(editor.querySelector('[aria-label="80% remaining"]')).toBeInTheDocument()
    else expect(editor).toHaveTextContent('80%')
    if (family === 'foundry') expect(editor.querySelector('.f-swatch')).toHaveStyle({ backgroundColor: '#112233' })
    else expect(editor.querySelector('circle[stroke="#112233"]')).toBeInTheDocument()
  })
  expect(rows[0]).toHaveTextContent('ID 255')
  expect(rows[8]).not.toHaveTextContent('Hand-fed')
  expect(rows[4]).toHaveTextContent('In the tool head')
  expect(Array.from(rows).filter(row => row.textContent?.includes('In the tool head'))).toHaveLength(1)
  expect(rack).not.toHaveTextContent(/provisional|Confirm spool/i)
  expect(rack).not.toHaveTextContent(/No AMS connected|AMS status unknown/)
  expect(rack).not.toHaveTextContent(/mounted|active nozzle|161|129/i)
  expect(screen.queryByRole('button', { name: 'Skip objects' })).not.toBeInTheDocument()
})

it.each([
  ['classic', 'bambu'], ['foundry', 'bambu'], ['classic', null], ['foundry', null],
])('edits an ODIN rack slot before any nozzle report in %s (protocol %s)', async (family, apiType) => {
  const { printers } = await import('../../api')
  const slots = Array.from({ length: 9 }, (_, index) => ({
    id: index + 1, slot_number: index + 1, filament_type: 'PETG',
    rack_filament: { id: 255 - index, type: 'PLA', preset: 'GFA01', color: 'AE96D4FF', observed_at: '2026-11-21T19:57:00Z' },
  }))
  vi.mocked(printers.get).mockResolvedValueOnce({
    id: 9, name: 'Rack printer', api_type: apiType, model: 'H3C', filament_slots: slots,
  } as Awaited<ReturnType<typeof printers.get>>)
  document.documentElement.dataset.themeFamily = family
  mount()
  await screen.findByText('No rack report received.')
  expect(screen.queryByRole('button', { name: 'Configure AMS tray' })).not.toBeInTheDocument()
  const editors = screen.getAllByTestId('filament-slot')
  expect(editors).toHaveLength(9)
  expect(editors[7].parentElement).toHaveTextContent('Hand-fed at the tool: PLA, GFA01, #AE96D4')
  fireEvent.click(editors[7])
  fireEvent.click(await screen.findByRole('button', { name: 'Clear' }))
  await waitFor(() => expect(printers.updateSlot).toHaveBeenCalledWith(9, 8, {
    filament_type: 'empty', color: null, color_hex: null, spoolman_spool_id: null,
  }))
})


it.each(['classic', 'foundry'])('offers the H3C Experimental switch for this printer in %s', async family => {
  const { printers } = await import('../../api')
  const { printerSupport } = await import('../../api/printerSupport')
  const save = vi.spyOn(printerSupport, 'setExperimentalActions').mockResolvedValue({
    printer_id: 9, support_level: 'supported', experimental_actions_enabled: true,
  })
  const rack = { id: 9, name: 'Rack', model: 'H3C', api_type: 'bambu', gcode_state: 'RUNNING',
    last_seen: new Date().toISOString(), filament_slots: [] } as Awaited<ReturnType<typeof printers.get>>
  vi.mocked(printers.get).mockResolvedValueOnce(rack).mockResolvedValueOnce({ ...rack, experimental_actions_enabled: true })
  Object.assign(state, { role: 'admin', group_id: null })
  document.documentElement.dataset.themeFamily = family
  mount('#experimental-opt-in-9')
  const toggle = await screen.findByRole('checkbox', { name: 'Allow job sending and controls (Experimental)' })
  expect(screen.getByText('H3C monitoring and filament tracking are Supported. Job sending and printer controls are Experimental and have not yet been verified on a printer.')).toBeVisible()
  expect(screen.queryByText(/Other supported controls were verified/)).not.toBeInTheDocument()
  expect(toggle).not.toBeChecked()
  expect(screen.getByText(/Job sending and controls are off/)).toHaveTextContent(/Experimental features.*Pause and stop/)
  expect(screen.queryByRole('button', { name: 'Skip objects' })).not.toBeInTheDocument()
  fireEvent.click(toggle)
  await waitFor(() => expect(save).toHaveBeenCalledWith(9, true))
  await waitFor(() => expect(toggle).toBeChecked())
  expect(await screen.findByRole('button', { name: 'Skip objects' })).toBeInTheDocument()
  save.mockRestore()
})

it.each(['classic', 'foundry'])('adds no support text to a P1S page in %s', async family => {
  const { printers } = await import('../../api')
  const source = 'Bambu Lab LAN-mode MQTT and FTPS. Skipping objects was verified on a P2S. Other supported controls were verified on real hardware.'
  vi.mocked(printers.get).mockResolvedValueOnce({
    id: 9, name: 'Printer', api_type: 'bambu', model: 'P1S', support_source: source,
  } as Awaited<ReturnType<typeof printers.get>>)
  document.documentElement.dataset.themeFamily = family
  mount()
  await screen.findByRole('heading', { level: 1 })
  expect(screen.queryByText(source)).not.toBeInTheDocument()
  expect(screen.queryByText(/H3C monitoring/)).not.toBeInTheDocument()
})
