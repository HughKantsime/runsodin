import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { ScheduleModal } from './Models'
import { useAmsMapping } from '../../components/shared/AmsMappingEditor'
import type { ArchiveAmsPreview } from '../../types/archive'
import wire from '../../test-fixtures/schedule-ams-wire.json'
import s1GcodePreview from '../../test-fixtures/s1-gcode-preview-wire.json'

const api = vi.hoisted(() => ({ file: vi.fn(), confirm: vi.fn() }))
vi.mock('../../api', () => ({
  models: { getVariants: async () => ({ variants: [] }), scheduleOptions: async () => ({ print_file_id: 501, payload_sha256: 'a'.repeat(64), plate_indices: [0] }), plates: async () => ({ plates: [] }) },
  printers: { list: async () => [{ id: 9, name: 'Bambu A1', api_type: 'bambu', is_active: true }, { id: 10, name: 'Other Bambu', api_type: 'bambu', is_active: true }, { id: 13, name: 'Kobra S1', model: 'Anycubic Kobra S1', api_type: 'moonraker', is_active: true }, { id: 12, name: 'Voron', api_type: 'moonraker', is_active: true }] },
  printFiles: { get: api.file }, filaments: {}, getApprovalSetting: vi.fn(),
}))
vi.mock('../../permissions', () => ({ canDo: () => true }))
beforeEach(() => { vi.clearAllMocks(); api.file.mockResolvedValue({ ams_preview: wire.preview }) })
afterEach(cleanup)
function mount() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <ScheduleModal isOpen onClose={vi.fn()} model={{ id: 501, name: 'Mapping print' }} onConfirm={api.confirm} isScheduling={false} />
  </QueryClientProvider>)
}

test('schedule shows source and loaded slots, prefills suggestion and sends confirmed edit', async () => {
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Bambu A1' }))
  const slot = await screen.findByLabelText('Filament 1 AMS slot')
  expect(slot).toHaveValue('4')
  expect(api.file).toHaveBeenCalledWith(501, 9)
  expect(api.confirm).not.toHaveBeenCalled()
  fireEvent.change(slot, { target: { value: '3' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create Job' }))
  expect(api.confirm).toHaveBeenCalledWith(9, expect.objectContaining({ ams_mapping: [{ source_slot: 1, target_slot: 3 }] }))
})

test('changing printers discards edited slots; non-Bambu and auto-assign never send a mapping', async () => {
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Bambu A1' }))
  fireEvent.change(await screen.findByLabelText('Filament 1 AMS slot'), { target: { value: '3' } })
  fireEvent.click(screen.getByRole('button', { name: 'Other Bambu' }))
  await waitFor(() => expect(screen.getByLabelText('Filament 1 AMS slot')).toHaveValue('4'))
  fireEvent.click(screen.getByRole('button', { name: 'Voron' }))
  expect(screen.queryByLabelText('Filament 1 AMS slot')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Create Job' }))
  expect(api.confirm.mock.calls[0][1]).not.toHaveProperty('ams_mapping')
  fireEvent.click(screen.getByRole('button', { name: 'Auto-assign' }))
  fireEvent.click(screen.getByRole('button', { name: 'Create Job' }))
  expect(api.confirm.mock.calls[1][1]).not.toHaveProperty('ams_mapping')
})

test('incomplete manual mapping blocks confirmation until completed or opted out', async () => {
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Bambu A1' }))
  fireEvent.change(await screen.findByLabelText('Filament 1 AMS slot'), { target: { value: '' } })
  expect(screen.getByRole('button', { name: 'Create Job' })).toBeDisabled()
  fireEvent.click(screen.getByLabelText('Use this mapping'))
  fireEvent.click(screen.getByRole('button', { name: 'Create Job' }))
  expect(api.confirm.mock.calls[0][1]).not.toHaveProperty('ams_mapping')
})

test('preview failure is visible and blocks accidental scheduling', async () => {
  api.file.mockRejectedValue(new Error('Unavailable'))
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Bambu A1' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Filament mapping preview unavailable')
  expect(screen.getByRole('button', { name: 'Create Job' })).toBeDisabled()
})


test('S1 supports explicit mapping while remaining a Moonraker printer', async () => {
  api.file.mockResolvedValue({ ams_preview: { ...wire.preview, available: [{ ...wire.preview.available[0], slot: 2 }], suggested_mapping: [{ source_slot: 1, target_slot: 2, confidence: 'material_only' }] } })
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Kobra S1' }))
  fireEvent.change(await screen.findByLabelText('Filament 1 AMS slot'), { target: { value: '2' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create Job' }))
  expect(api.confirm).toHaveBeenCalledWith(13, expect.objectContaining({ ams_mapping: [{ source_slot: 1, target_slot: 2 }] }))
})

// Captured from the real upload and preview routes by test_kobra_s1_gcode_pipeline.
test('an uploaded S1 gcode lists its filaments and creates a job with the confirmed mapping', async () => {
  api.file.mockResolvedValue({ ams_preview: s1GcodePreview })
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Kobra S1' }))
  const first = await screen.findByLabelText('Filament 1 AMS slot')
  const second = screen.getByLabelText('Filament 2 AMS slot')
  expect(screen.getByRole('button', { name: 'Create Job' })).toBeEnabled()
  fireEvent.change(first, { target: { value: '2' } })
  fireEvent.change(second, { target: { value: '3' } })
  fireEvent.click(screen.getByRole('button', { name: 'Create Job' }))
  expect(api.file).toHaveBeenCalledWith(501, 13)
  expect(api.confirm).toHaveBeenCalledWith(13, expect.objectContaining({ ams_mapping: [{ source_slot: 1, target_slot: 2 }, { source_slot: 2, target_slot: 3 }] }))
})

test('a material-only suggestion on an S1 stays off until someone picks or confirms the slots', async () => {
  api.file.mockResolvedValue({ ams_preview: s1GcodePreview })
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Kobra S1' }))
  await screen.findByLabelText('Filament 1 AMS slot')
  expect(screen.getByLabelText('Use this mapping')).not.toBeChecked()
  fireEvent.click(screen.getByRole('button', { name: 'Create Job' }))
  expect(api.confirm.mock.calls[0][1]).not.toHaveProperty('ams_mapping')
  fireEvent.click(screen.getByLabelText('Use this mapping'))
  fireEvent.click(screen.getByRole('button', { name: 'Create Job' }))
  expect(api.confirm.mock.calls[1][1]).toHaveProperty('ams_mapping')
})

test('same-material S1 slots can be told apart by colour', async () => {
  api.file.mockResolvedValue({ ams_preview: s1GcodePreview })
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Kobra S1' }))
  const slot = await screen.findByLabelText('Filament 1 AMS slot')
  const labels = Array.from(slot.querySelectorAll('option')).map(option => option.textContent)
  expect(labels).toEqual(expect.arrayContaining(['Slot 1 (PLA #00BB31)', 'Slot 2 (PLA #6558B1)', 'Slot 3 (PLA #EF60A3)', 'Slot 4 (PLA #0047BB)']))
})

test('an S1 file that cannot be mapped says why and still allows a job without a mapping', async () => {
  api.file.mockResolvedValue({ ams_preview: { required: [], available: s1GcodePreview.available, suggested_mapping: [], file_note: 'S1 files need a Bambu Studio header' } })
  mount(); fireEvent.click(await screen.findByRole('button', { name: 'Kobra S1' }))
  expect(await screen.findByText('S1 files need a Bambu Studio header')).toBeInTheDocument()
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Create Job' }))
  expect(api.confirm.mock.calls[0][1]).not.toHaveProperty('ams_mapping')
})

test('picking slots turns a mapping on only where a suggestion was held back for confirmation', () => {
  const slots = s1GcodePreview.available
  const required = [{ slot: 1, color: '#00AE42', material: 'PLA' }]
  const pick = [{ source_slot: 1, target_slot: 2 }]
  const bambu = renderHook(() => useAmsMapping({ required, available: slots, suggested_mapping: [] }, 'bambu'))
  expect(bambu.result.current.enabled).toBe(false)
  act(() => bambu.result.current.onChange(pick))
  expect(bambu.result.current.mapping).toEqual(pick)
  expect(bambu.result.current.enabled).toBe(false)
  expect(bambu.result.current.value).toBeUndefined()
  const s1 = renderHook(() => useAmsMapping({ required, available: slots, suggested_mapping: [], confirm_suggestions: true }, 's1'))
  act(() => s1.result.current.onChange(pick))
  expect(s1.result.current.value).toEqual(pick)
})

test('a mapping someone already saved stays on, even where a new suggestion would be held back', () => {
  const { result } = renderHook(() => useAmsMapping(s1GcodePreview as ArchiveAmsPreview, 'job', [1, 2]))
  expect(result.current.enabled).toBe(true)
  expect(result.current.value).toEqual([{ source_slot: 1, target_slot: 2 }, { source_slot: 2, target_slot: 3 }])
})
