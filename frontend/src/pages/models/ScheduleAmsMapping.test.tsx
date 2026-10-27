import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { ScheduleModal } from './Models'
import wire from '../../test-fixtures/schedule-ams-wire.json'

const api = vi.hoisted(() => ({ file: vi.fn(), confirm: vi.fn() }))
vi.mock('../../api', () => ({
  models: { getVariants: async () => ({ variants: [] }), scheduleOptions: async () => ({ print_file_id: 501, payload_sha256: 'a'.repeat(64), plate_indices: [0] }), plates: async () => ({ plates: [] }) },
  printers: { list: async () => [{ id: 9, name: 'Bambu A1', api_type: 'bambu', is_active: true }, { id: 10, name: 'Other Bambu', api_type: 'bambu', is_active: true }, { id: 12, name: 'Voron', api_type: 'moonraker', is_active: true }] },
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
  expect(api.confirm).toHaveBeenCalledWith(9, expect.objectContaining({ ams_mapping: [{ source_slot: 0, target_slot: 3 }] }))
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
