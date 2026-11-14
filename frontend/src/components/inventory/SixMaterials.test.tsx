import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { EditFilamentModal, EditSpoolModal } from './SpoolEditModals'
import FilamentSlotEditor from '../printers/FilamentSlotEditor'
import { previewCsv } from './receivingCsv'

vi.mock('../../api', () => ({ printers: { assignSlotSpool: vi.fn(), confirmTrayIdentity: vi.fn() }, spools: { create: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: () => true, getCurrentUser: () => ({ role: 'operator' }) }))
const six = [
  ['ASA-AERO', 'ASA Aero'], ['ASA-CF', 'ASA-CF'], ['PET-CF', 'PET-CF'],
  ['PPA-CF', 'PPA-CF'], ['PPS-CF', 'PPS-CF'], ['TPU-AMS', 'TPU for AMS'],
]
afterEach(() => { cleanup(); document.documentElement.dataset.themeFamily = 'classic'; vi.unstubAllGlobals() })

for (const theme of ['classic', 'foundry']) {
  it.each(six)(`${theme}: library select saves %s with its display name`, (material, label) => {
    document.documentElement.dataset.themeFamily = theme
    const onSave = vi.fn()
    render(<EditFilamentModal filament={{ id: 'lib_5', brand: 'Fixture', name: 'Blue', material: 'PLA' }} onClose={() => {}} onSave={onSave} />)
    const option = screen.getByRole('option', { name: label })
    expect(option).toHaveValue(material)
    fireEvent.change(option.parentElement!, { target: { value: material } })
    fireEvent.submit(option.closest('form')!)
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ material }))
  })

  it.each(six)(`${theme}: slot editor selects %s at phone width`, (material, label) => {
    document.documentElement.dataset.themeFamily = theme
    vi.stubGlobal('innerWidth', 390)
    const onSave = vi.fn()
    render(<div style={{ width: 390 }}><FilamentSlotEditor printerId={1}
      slot={{ id: 1, slot_number: 1, material_type: 'ASA', color_hex: '123456' }}
      allFilaments={[{ id:'lib_5', source:'library', brand:'Fixture', name:label, display_name:`Fixture ${label}`, material, color_hex:'123456' }]}
      spools={[]} onSave={onSave} /></div>)
    fireEvent.click(screen.getByTestId('filament-slot'))
    const dialog = screen.getByRole('dialog')
    expect(dialog.children[1]).toHaveClass('w-full', 'max-sm:h-full', 'max-sm:max-h-none')
    fireEvent.click(within(dialog).getByRole('button', { name: new RegExp(`Fixture ${label}`) }))
    expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ filament_type: material }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it.each(six)(`${theme}: spool picker retains exact %s catalogue choices`, (material, label) => {
    document.documentElement.dataset.themeFamily = theme
    render(<EditSpoolModal spool={{ id:1, filament_id:2, filament_material:material, filament_name:label }}
      filaments={[{ id:2,name:label,brand:'Fixture',material },
        { id:3,name:`Catalogue ${label}`,brand:'Fixture',material,catalogue_code:'SYNTHETIC' },
        { id:4,name:'Wrong family',brand:'Fixture',material:'PLA',catalogue_code:'SYNTHETIC-WRONG' }]}
      onClose={() => {}} onSave={vi.fn()} />)
    expect(screen.getByRole('option', { name: new RegExp(`Catalogue ${label}`) })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /Wrong family/ })).not.toBeInTheDocument()
  })
}

it.each(six)('CSV receiving accepts %s and preserves exact identity', (material, label) => {
  const [preview] = previewCsv(`brand,product_line,material,color_name,storage_location\nFixture,${label},${material},Blue,Shelf`, [])
  expect(preview.errors).toEqual([])
  expect(preview.row.material).toBe(material)
})

it.each(six)('existing %s enum spelling selects the correct library option', (material) => {
  render(<EditFilamentModal filament={{ id:5, brand:'Fixture', name:'Blue', material:material.replace(/-/g,'_') }} onClose={() => {}} onSave={vi.fn()} />)
  expect(screen.getByRole('combobox')).toHaveValue(material)
})
