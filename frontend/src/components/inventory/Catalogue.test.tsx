import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import CataloguePicker from './CataloguePicker'
import FilamentLibraryView from './FilamentLibraryView'
import { CreateSpoolModal } from './SpoolModals'
import { EditFilamentModal, EditSpoolModal } from './SpoolEditModals'
import FilamentSlotEditor from '../printers/FilamentSlotEditor'
import FilamentCatalogueSettings from '../admin/FilamentCatalogueSettings'
import { filaments } from '../../api/inventory'
import { printers } from '../../api'

const auth = vi.hoisted(() => ({ role: 'admin', group_id: null as number | null }))
vi.mock('../../api/inventory', () => ({ filaments: { list: vi.fn(), importBambu: vi.fn(), chooseSlot: vi.fn() } }))
vi.mock('../../api', async () => ({ printers: { assignSlotSpool: vi.fn() }, spools: {}, filaments: (await import('../../api/inventory')).filaments }))
vi.mock('../../permissions', () => ({ getCurrentUser: () => auth, canDo: () => true }))
const entries = [
  { id: 'lib_12', brand: 'Bambu Lab', name: 'PLA Matte Invented Orbit', material: 'PLA', color_hex: '1234AB', catalogue_type: 'PLA Matte', catalogue_code: 'ODIN-TEST-ORBIT' },
  { id: 'lib_13', brand: 'Bambu Lab', name: 'PLA Silk+ Invented Twilight', material: 'PLA', color_hex: '1234AB', catalogue_type: 'PLA Silk+', catalogue_code: 'ODIN-TEST-TWILIGHT' },
  { id: 'lib_14', brand: 'Synthetic', name: 'Custom Comet', material: 'PLA', color_hex: '789ABC' },
].map(f => ({ ...f, source: 'library', display_name: `${f.brand} ${f.name}` }))
const spool = { id: 7, filament_id: 14, filament_name: 'Custom Comet', filament_material: 'PLA', remaining_weight_g: 500, initial_weight_g: 1000, spool_weight_g: 250 }
const slot = { id: 1, slot_number: 1, color_hex: '1234AB', material_type: 'PLA', remaining: null, assigned_spool_id: 7, spool_confirmed: true }
function provider(child: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  client.setQueryData(['auth', 'me'], { id: 3, role: 'admin', group_id: null })
  return render(<QueryClientProvider client={client}>{child}</QueryClientProvider>)
}
beforeEach(() => { auth.role = 'admin'; auth.group_id = null; vi.clearAllMocks(); vi.mocked(filaments.chooseSlot).mockResolvedValue({ success: true }) })
afterEach(() => { cleanup(); delete document.documentElement.dataset.themeFamily })

for (const theme of ['classic', 'foundry']) {
  it(`${theme}: searches names, type and colour and keeps custom filaments`, () => {
    document.documentElement.dataset.themeFamily = theme
    render(<CataloguePicker filaments={entries} value="" onChange={vi.fn()} />)
    const search = screen.getByRole('searchbox')
    for (const term of ['Invented Orbit', 'PLA Matte', '1234AB']) {
      fireEvent.change(search, { target: { value: term } })
      expect(screen.getByRole('option', { name: /PLA Matte Invented Orbit/ })).toBeInTheDocument()
      expect(screen.queryByRole('option', { name: /Custom Comet/ })).not.toBeInTheDocument()
    }
    fireEvent.change(search, { target: { value: 'Custom Comet' } })
    expect(screen.getByRole('option', { name: /Custom Comet/ })).toBeInTheDocument()
  })
  it(`${theme}: create and edit remember the selected filament id`, () => {
    document.documentElement.dataset.themeFamily = theme
    const create = vi.fn(), save = vi.fn()
    const view = provider(<CreateSpoolModal filaments={entries} onClose={vi.fn()} onCreate={create} />)
    fireEvent.change(screen.getByLabelText('Filament'), { target: { value: '12' } })
    expect(screen.getByText(/Bambu Lab · PLA Matte Invented Orbit · PLA · #1234AB/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Add Spool' }))
    expect(create).toHaveBeenCalledWith(expect.objectContaining({ filament_id: 12 }), expect.any(String))
    view.unmount()
    provider(<EditSpoolModal spool={spool} filaments={entries} onClose={vi.fn()} onSave={save} />)
    fireEvent.change(screen.getByLabelText('Filament'), { target: { value: '13' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(save).toHaveBeenCalledWith(expect.objectContaining({ id: 7, filament_id: 13 }))
  })
  it(`${theme}: unique label, ambiguous choice, and unmatched fallback at phone width`, async () => {
    document.documentElement.dataset.themeFamily = theme
    Object.defineProperty(window, 'innerWidth', { value: 375, configurable: true })
    const save = vi.fn()
    const view = render(<FilamentSlotEditor slot={{ ...slot, catalogue_name: entries[0].name }} allFilaments={entries} spools={[]} printerId={9} apiType="anycubic_lan" onSave={save} />)
    expect(screen.getByText(entries[0].name)).toBeInTheDocument()
    view.rerender(<FilamentSlotEditor slot={{ ...slot, catalogue_choices: entries.slice(0, 2).map(f => ({ id: Number(f.id.slice(4)), name: f.name })) }} allFilaments={entries} spools={[]} printerId={9} apiType="anycubic_lan" onSave={save} />)
    expect(screen.getByText('Choose catalogue name')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('filament-slot'))
    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveClass('fixed', 'inset-0')
    fireEvent.click(screen.getByRole('button', { name: entries[1].name }))
    await waitFor(() => expect(filaments.chooseSlot).toHaveBeenCalledWith(9, 1, 7, 13))
    expect(save).toHaveBeenCalledWith({})
    view.rerender(<FilamentSlotEditor slot={slot} allFilaments={entries} spools={[]} printerId={9} onSave={save} />)
    expect(screen.getByText('PLA')).toBeInTheDocument()
  })
}

it('imports a supplied file and reports unknown types', async () => {
  vi.mocked(filaments.importBambu).mockResolvedValue({ created: 3, updated: 0, preserved: 0, skipped: [], unrecognised_types: ['Synthetic Polymer'] })
  provider(<FilamentCatalogueSettings />)
  expect(screen.getByText(/from your own Bambu Studio installation/)).toBeInTheDocument()
  const file = new File(['{"total":0,"data":[]}'], 'invented.json', { type: 'application/json' })
  fireEvent.change(screen.getByLabelText('Catalogue file'), { target: { files: [file] } })
  fireEvent.click(screen.getByRole('button', { name: 'Import Bambu catalogue' }))
  await waitFor(() => expect(filaments.importBambu).toHaveBeenCalledWith(file, expect.anything()))
  expect(await screen.findByRole('status')).toHaveTextContent('Synthetic Polymer')
})

it('shows shared-library access rules to a tenant administrator', () => {
  auth.group_id = 1
  provider(<FilamentCatalogueSettings />)
  expect(screen.getByText(/Ask an installation administrator/)).toBeInTheDocument()
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
})

it('rejects an oversized file without sending it', () => {
  provider(<FilamentCatalogueSettings />)
  const file = new File(['x'.repeat(1024 * 1024 + 1)], 'invented.json')
  fireEvent.change(screen.getByLabelText('Catalogue file'), { target: { files: [file] } })
  fireEvent.click(screen.getByRole('button', { name: 'Import Bambu catalogue' }))
  expect(screen.getByRole('alert')).toHaveTextContent('smaller than 1 MB')
  expect(filaments.importBambu).not.toHaveBeenCalled()
})


it('lists and filters an imported type without a recognised base', async () => {
  vi.mocked(filaments.list).mockResolvedValue([...entries, { id: 'lib_15', brand: 'Bambu Lab', name: 'Invented Unlisted Polymer', material: 'OTHER' }] as any)
  provider(<FilamentLibraryView />)
  expect(await screen.findByText('Invented Unlisted Polymer')).toBeInTheDocument()
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'OTHER' } })
  expect(screen.getByText('Invented Unlisted Polymer')).toBeInTheDocument()
  expect(screen.queryByText('PLA Matte Invented Orbit')).not.toBeInTheDocument()
})

for (const theme of ['classic', 'foundry']) {
  it(`${theme}: a catalogue pick still sets a manual slot's material and colour`, async () => {
    document.documentElement.dataset.themeFamily = theme
    const save = vi.fn()
    render(<FilamentSlotEditor slot={{ ...slot, assigned_spool_id: null }} allFilaments={entries} spools={[]} printerId={9} apiType="moonraker" onSave={save} />)
    fireEvent.click(screen.getByTestId('filament-slot'))
    fireEvent.click(screen.getByRole('button', { name: /Bambu Lab PLA Matte Invented Orbit/ }))
    expect(save).toHaveBeenCalledWith({ color: 'Bambu Lab PLA Matte Invented Orbit', color_hex: '1234AB', filament_type: 'PLA', spoolman_spool_id: null })
    expect(filaments.chooseSlot).not.toHaveBeenCalled()
  })

  it(`${theme}: a catalogue-linked spool still fills a manual slot, including OTHER`, async () => {
    document.documentElement.dataset.themeFamily = theme
    const save = vi.fn()
    const linked = { ...spool, catalogue_code: 'ODIN-TEST-COMET', filament_brand: 'Bambu Lab', filament_name: 'Invented Comet', filament_color_hex: 'ABC123', filament_material: 'OTHER' }
    render(<FilamentSlotEditor slot={{ ...slot, assigned_spool_id: null }} allFilaments={[]} spools={[linked]} printerId={9} apiType="moonraker" onSave={save} />)
    fireEvent.click(screen.getByTestId('filament-slot'))
    fireEvent.click(screen.getByRole('button', { name: /Bambu Lab Invented Comet/ }))
    await waitFor(() => expect(save).toHaveBeenCalledWith({ color: 'Bambu Lab Invented Comet', color_hex: 'ABC123', filament_type: 'OTHER', assigned_spool_id: 7 }))
    expect(printers.assignSlotSpool).toHaveBeenCalledWith(9, 1, 7)
  })
}

it('editing an imported unknown type retains OTHER; the six newly recognised materials are offered', () => {
  const save = vi.fn()
  const view = render(<EditFilamentModal filament={{ ...entries[0], material: 'OTHER' }} onSave={save} onClose={vi.fn()} />)
  expect(screen.getByRole('combobox')).toHaveValue('OTHER')
  expect(screen.getByRole('combobox')).toBeDisabled()
  for (const label of ['ASA-AERO','ASA-CF','PET-CF','PPA-CF','TPU-AMS']) {
    expect(screen.getAllByRole('option').some(option => (option as HTMLOptionElement).value === label)).toBe(true)
  }
  // PPS-CF was already a choice before the other five; what it stores is unchanged.
  expect(screen.getByRole('option', { name: 'PPS-CF', exact: true })).toHaveValue('PPS_CF')
  fireEvent.submit(view.container.ownerDocument.querySelector('form')!)
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ material: 'OTHER' }))
})

it.each(['missing', 'spoolman', 'empty'])('saves unrelated spool edits when current filament is %s', kind => {
  const save = vi.fn()
  const list = kind === 'empty' ? [] : kind === 'spoolman'
    ? [{ ...entries[0], id: 'spool_14', source: 'spoolman' }] : entries.slice(0, 2)
  provider(<EditSpoolModal spool={spool} filaments={list} onClose={vi.fn()} onSave={save} />)
  fireEvent.change(screen.getByPlaceholderText('Any notes...'), { target: { value: 'New note' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ notes: 'New note' }))
  expect(save.mock.calls[0][0]).not.toHaveProperty('filament_id')
  if (list.length) expect(screen.getByRole('option', { selected: true })).toHaveTextContent('Custom Comet')
})

it('unknown spool-change state blocks catalogue and assignment actions without offering confirmation', () => {
  const save = vi.fn()
  render(<FilamentSlotEditor slot={{ ...slot, spool_change_pending: true, observed_state: null,
    catalogue_choices: [{ id: 12, name: entries[0].name }] }} allFilaments={entries} spools={[]} printerId={9} onSave={save} />)
  fireEvent.click(screen.getByTestId('filament-slot'))
  expect(screen.getByRole('button', { name: entries[0].name })).toBeDisabled()
  expect(screen.queryByPlaceholderText('Search filaments...')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: entries[0].name }))
  expect(filaments.chooseSlot).not.toHaveBeenCalled()
  expect(save).not.toHaveBeenCalled()
})

it.each(['classic', 'foundry'])('%s: edit offers only the current filament and same-material catalogue entries', family => {
  document.documentElement.dataset.themeFamily = family
  const choices = [
    ...entries,
    { id: 'lib_15', name: 'Wrong material', material: 'PETG', catalogue_code: 'ODIN-TEST-WRONG' },
    { id: 'lib_16', name: 'Another custom PLA', material: 'PLA' },
    { id: 'spool_17', name: 'External PLA', material: 'PLA', source: 'spoolman', catalogue_code: 'ODIN-TEST-EXTERNAL' },
    { id: 'lib_18', name: 'Lowercase PLA', material: ' pla ', catalogue_code: 'ODIN-TEST-LOWER' },
  ]
  const save = vi.fn()
  provider(<EditSpoolModal spool={{ ...spool, filament_material: 'PLA' }} filaments={choices} onClose={vi.fn()} onSave={save} />)
  expect(screen.getAllByRole('option').map(o => (o as HTMLOptionElement).value)).toEqual(['', '12', '13', '14', '18'])
  expect(screen.getByText('Only the current filament and catalogue entries with the same material are shown.')).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Filament'), { target: { value: '18' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
  expect(save).toHaveBeenCalledWith(expect.objectContaining({ filament_id: 18 }))
})

it.each(['OTHER', 'Invented polymer', undefined])('unknown material %s keeps only the current filament', material => {
  provider(<EditSpoolModal spool={{ ...spool, filament_material: material }} filaments={[
    { id: 'lib_15', name: 'Unrecognised catalogue entry', material, catalogue_code: 'ODIN-TEST-UNKNOWN' },
  ]} onClose={vi.fn()} onSave={vi.fn()} />)
  expect(screen.getAllByRole('option').map(o => (o as HTMLOptionElement).value)).toEqual(['', '14'])
})


it('shows which catalogue entries were skipped because their material changed', async () => {
  vi.mocked(filaments.importBambu).mockResolvedValue({ created: 0, updated: 2, preserved: 1,
    skipped: [{ catalogue_code: 'ODIN-TEST-ORBIT', reason: 'type_changed' }], unrecognised_types: [] })
  provider(<FilamentCatalogueSettings />)
  fireEvent.change(screen.getByLabelText('Catalogue file'), { target: { files: [new File(['{}'], 'invented.json')] } })
  fireEvent.click(screen.getByRole('button', { name: 'Import Bambu catalogue' }))
  expect(await screen.findByRole('status')).toHaveTextContent('Skipped because the material type changed: ODIN-TEST-ORBIT. Existing entries were left unchanged.')
})
