import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { ScheduleModal } from './Models'

// The schedule dialog pre-selects the printer when exactly one fits (the server's count),
// and never replaces a choice the person already made.
const api = vi.hoisted(() => ({ variants: vi.fn(), confirm: vi.fn() }))
vi.mock('../../api', () => ({
  models: { getVariants: api.variants, scheduleOptions: async () => ({ print_file_id: 501, payload_sha256: 'a'.repeat(64), plate_indices: [0] }), plates: async () => ({ plates: [] }) },
  printers: { list: async () => [{ id: 12, name: 'Voron', api_type: 'moonraker', is_active: true }, { id: 14, name: 'Ender', api_type: 'moonraker', is_active: true }] },
  printFiles: { get: vi.fn() }, filaments: {}, getApprovalSetting: vi.fn(),
}))
vi.mock('../../permissions', () => ({ canDo: () => true }))
beforeEach(() => vi.clearAllMocks())
afterEach(cleanup)

const facts = { nozzle_diameter: 0.4, layer_height: 0.2, plate_type: 'Textured PEI Plate', supports: true, filament_colors: ['#FFFFFF'] }
const variant = { id: 501, filename: 'part.gcode', printer_model: 'Voron 2.4', bed_x_mm: 350, bed_y_mm: 350, compatible_api_types: 'moonraker', file_facts: facts }
const one = { count: 1, printers: [{ id: 14, name: 'Ender' }], sliced_for: 'Voron 2.4' }
const two = { count: 2, printers: [{ id: 12, name: 'Voron' }, { id: 14, name: 'Ender' }], sliced_for: 'Voron 2.4' }

function mount() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <ScheduleModal isOpen onClose={vi.fn()} model={{ id: 501, name: 'Part' }} onConfirm={api.confirm} isScheduling={false} />
  </QueryClientProvider>)
}
const selected = (name: string) => screen.getByRole('button', { name }).className.includes('text-[var(--brand-primary)]')
async function create() {
  const button = screen.getByRole('button', { name: 'Create Job' })
  await waitFor(() => expect(button).toBeEnabled())
  fireEvent.click(button)
  return api.confirm.mock.calls[api.confirm.mock.calls.length - 1][0]
}

test('exactly one fitting printer is pre-selected', async () => {
  api.variants.mockResolvedValue({ variants: [variant], printer_fit: one })
  mount()
  await waitFor(() => expect(selected('Ender')).toBe(true))
  expect(selected('Auto-assign')).toBe(false)
  expect(await create()).toBe(14)
})

test('several fitting printers, none, or no answer leave Auto-assign selected', async () => {
  for (const printer_fit of [two, { count: 0, printers: [], sliced_for: 'Bambu Lab P2S' }, null, undefined]) {
    api.variants.mockResolvedValue({ variants: [variant], printer_fit })
    mount()
    await screen.findByText('Voron 2.4')
    await screen.findByRole('button', { name: 'Ender' })
    expect(selected('Auto-assign')).toBe(true)
    expect(await create()).toBeNull()
    cleanup()
  }
})

test('a choice made before the answer arrives is kept', async () => {
  let answer: (value: unknown) => void = () => {}
  api.variants.mockReturnValue(new Promise(resolve => { answer = resolve }))
  mount()
  fireEvent.click(await screen.findByRole('button', { name: 'Voron' }))
  answer({ variants: [variant], printer_fit: one })
  await screen.findByText('Voron 2.4')
  expect(selected('Voron')).toBe(true)
  expect(await create()).toBe(12)
})

test('choosing Auto-assign after the pre-selection is not undone', async () => {
  api.variants.mockResolvedValue({ variants: [variant], printer_fit: one })
  mount()
  await waitFor(() => expect(selected('Ender')).toBe(true))
  fireEvent.click(screen.getByRole('button', { name: 'Auto-assign' }))
  await waitFor(() => expect(selected('Auto-assign')).toBe(true))
  expect(selected('Ender')).toBe(false)
  expect(await create()).toBeNull()
})

test('the dialog shows the file facts with the existing chips, and nothing for missing ones', async () => {
  api.variants.mockResolvedValue({ variants: [variant, { ...variant, id: 502, printer_model: 'Prusa MK4', file_facts: null }], printer_fit: two })
  mount()
  const row = (await screen.findByText('Voron 2.4')).parentElement!
  expect(row).toHaveTextContent('350x350mm')
  for (const label of ['0.4 mm nozzle', '0.2 mm layers', 'Textured PEI Plate', 'Supports on', 'White']) expect(row).toHaveTextContent(label)
  expect(screen.getByText('Prusa MK4').parentElement).not.toHaveTextContent(/nozzle|layers|Supports/)
})
