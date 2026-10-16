// Cert B: archive tag URL state (F02-2.8-23) and the reprint AMS mapping preview (F02-2.8-26).
// The AMS preview response is the producer wire fixture written by
// tests/test_contracts/test_cert_B_archive_fixes.py::test_ams_preview_for_printer_with_slots_maps_distinct_trays.
import { render, screen, cleanup, fireEvent, within, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import wire from '../../test-fixtures/cert-B-wire.json'
import ArchivesPage from './Archives'

const state = vi.hoisted(() => ({ items: [] as any[], previewArgs: [] as any[], reprint: vi.fn() }))
vi.mock('@tanstack/react-query', () => ({
  useQuery: ({ queryKey, queryFn, enabled }: any) => {
    if (queryKey[0] === 'archives') return { data: { items: state.items, total: state.items.length }, isLoading: false, isError: false, refetch: vi.fn() }
    if (queryKey[0] === 'archive-ams-preview') {
      if (enabled === false) return { data: undefined, isLoading: false, isError: false }
      state.previewArgs.push(queryKey.slice(1)); queryFn()
      return { data: wire.archive_ams_preview, isLoading: false, isError: false }
    }
    if (queryKey[0] === 'printers-list') return { data: [{ id: 9, name: 'P1S-09', is_active: true, api_type: 'bambu' }, { id: 7, name: 'Voron', is_active: true, api_type: 'moonraker' }] }
    return { data: undefined }
  },
  useMutation: () => ({ mutate: vi.fn(), isPending: false }),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}))
vi.mock('../../permissions', () => ({ canDo: () => true }))
vi.mock('../../api', async (original) => {
  const actual: any = await original()
  return { ...actual, archives: { ...actual.archives, amsPreview: vi.fn(async () => wire.archive_ams_preview), reprint: (...args: any[]) => state.reprint(...args) } }
})

let location: { search: string } = { search: '' }
function Where() { location = useLocation(); return null }
function mount(entry = '/archives') {
  return render(<MemoryRouter initialEntries={[entry]}><ArchivesPage /><Where /></MemoryRouter>)
}
const archive = (over: any = {}) => ({ id: 101, print_name: 'Bracket', printer_display: 'P1S-09', status: 'completed', actual_duration_seconds: 60,
  filament_used_grams: 3, tags: ['alpha', 'beta'], started_at: null, completed_at: null, file_path: '/data/uploads/bracket.3mf', print_file_id: 101, plate_count: 1, ...over })

beforeEach(() => { state.items = [archive()]; state.previewArgs = []; state.reprint = vi.fn(async () => ({ message: 'Reprint job created' })) })
afterEach(() => cleanup())

test('tag click writes tag= to the URL and resets the page in one navigation', async () => {
  mount('/archives?page=2&status=failed')
  fireEvent.click(screen.getByRole('button', { name: 'alpha' }))
  await waitFor(() => expect(new URLSearchParams(location.search).get('tag')).toBe('alpha'))
  const params = new URLSearchParams(location.search)
  expect(params.get('page')).toBeNull()          // page 1 is the default, not written
  expect(params.get('status')).toBe('failed')    // other filters survive
})

test('clearing the tag filter removes tag= and keeps other params', async () => {
  mount('/archives?tag=alpha&q=Bracket&page=3')
  // The active-filter chip precedes the table's tag chips.
  fireEvent.click(screen.getAllByRole('button', { name: 'alpha' })[0])
  await waitFor(() => expect(new URLSearchParams(location.search).get('tag')).toBeNull())
  expect(new URLSearchParams(location.search).get('page')).toBeNull()
  expect(new URLSearchParams(location.search).get('q')).toBe('Bracket')
})

test('reprint panel shows the AMS mapping preview for the chosen printer', () => {
  mount()
  fireEvent.click(screen.getByText('Bracket'))
  const dialog = within(screen.getByRole('dialog'))
  fireEvent.click(dialog.getByRole('button', { name: /Reprint/ }))
  expect(dialog.queryByTestId('reprint-ams-preview')).toBeNull()  // nothing until a printer is chosen
  fireEvent.change(dialog.getByRole('combobox'), { target: { value: '9' } })
  expect(state.previewArgs).toContainEqual([101, '9'])
  const preview = within(dialog.getByTestId('reprint-ams-preview'))
  expect(preview.getByText('Filament 1: PLA').closest('li')).toHaveTextContent('Slot 2 (Black PLA)')
  expect(preview.getByText('Filament 2: PLA').closest('li')).toHaveTextContent('Slot 1 (White PLA)')
  expect(preview.getByText('Filament 3: PETG').closest('li')).toHaveTextContent('No matching slot')
  // The fixture leaves PETG unmatched, so no (rejected) partial mapping is offered.
  expect(preview.queryByLabelText('Use this mapping')).toBeNull()
})

test('a complete suggested mapping is sent with the reprint for a Bambu printer', async () => {
  const complete = { ...wire.archive_ams_preview, required: wire.archive_ams_preview.required.slice(0, 2) }
  const original = wire.archive_ams_preview
  ;(wire as any).archive_ams_preview = complete
  try {
    mount()
    fireEvent.click(screen.getByText('Bracket'))
    const dialog = within(screen.getByRole('dialog'))
    fireEvent.click(dialog.getByRole('button', { name: /Reprint/ }))
    fireEvent.change(dialog.getByRole('combobox'), { target: { value: '9' } })
    expect(dialog.getByLabelText('Use this mapping')).toBeChecked()
    fireEvent.click(dialog.getAllByRole('button', { name: 'Reprint' }).slice(-1)[0])
    await waitFor(() => expect(state.reprint).toHaveBeenCalled())
    expect(state.reprint).toHaveBeenCalledWith(101, { printer_id: 9, plate_index: 0, ams_mapping: [{ source_slot: 0, target_slot: 2 }, { source_slot: 1, target_slot: 1 }] })
  } finally { (wire as any).archive_ams_preview = original }
})
