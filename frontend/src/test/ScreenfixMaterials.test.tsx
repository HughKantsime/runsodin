import { readFileSync } from 'node:fs'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { materialLabel } from '../utils/shared'
import type { FilamentType } from '../types/printer'
import SlotTile from '../components/foundry/SlotTile'
import { FilamentSlotsPanel } from '../components/printers/PrinterPanels'
import AmsMappingEditor from '../components/shared/AmsMappingEditor'
import SpoolCard from '../components/inventory/SpoolCard'
vi.mock('../permissions', () => ({ canDo: () => true }))
vi.mock('../components/inventory/PurchaseEvidenceModal', () => ({ default: () => null }))
const noop = () => {}
afterEach(() => { cleanup(); delete document.documentElement.dataset.themeFamily })
const labels: Record<FilamentType, string> = {
  empty: 'Empty', Unknown: 'Unknown', PLA: 'PLA', PETG: 'PETG', ABS: 'ABS', ASA: 'ASA', TPU: 'TPU', PA: 'PA', PC: 'PC', PVA: 'PVA', OTHER: 'Other',
  PLA_SUPPORT: 'PLA Support', PLA_CF: 'PLA-CF', PETG_CF: 'PETG-CF', NYLON_CF: 'NYLON-CF', NYLON_GF: 'NYLON-GF', PC_ABS: 'PC-ABS', PC_CF: 'PC-CF', SUPPORT: 'Support', HIPS: 'HIPS', PPS: 'PPS', PPS_CF: 'PPS-CF', ASA_AERO: 'ASA Aero', ASA_CF: 'ASA-CF', PET_CF: 'PET-CF', PPA_CF: 'PPA-CF', TPU_AMS: 'TPU for AMS',
}
it('every FilamentType has an explicit expected display label', () => {
  const types = readFileSync('src/types/printer.ts', 'utf8').split('export type FilamentType =')[1].split(';')[0]
  expect([...types.matchAll(/'([^']+)'/g)].map(m => m[1]).sort()).toEqual(Object.keys(labels).sort())
  for (const [code, label] of Object.entries(labels)) expect(materialLabel(code)).toBe(label)
})
it.each(['ASA_CF', 'TPU_AMS', 'ASA_AERO', 'PET_CF', 'PPA_CF', 'PPS_CF'] as FilamentType[])('%s renders labels in slots, Classic detail and spools', code => {
  const slot = { slot_number: 1, filament_type: code, material_type: code, color: code } as any
  const { container } = render(<><SlotTile slot={slot} expandable={false} /><FilamentSlotsPanel printer={{ filament_slots: [slot] } as any} />
    <SpoolCard spool={{ id: 1, filament_material: code, status: 'active' } as any} onLoad={noop} onUnload={noop} onUse={noop} onArchive={noop} onEdit={noop} onDry={noop} /></>)
  expect(container.textContent).not.toContain(code)
  expect(screen.getAllByText(labels[code]).length).toBeGreaterThanOrEqual(3)
})
it.each(['classic', 'foundry'])('%s mapping shows catalogue then material, and explicitly empty trays', family => {
  document.documentElement.dataset.themeFamily = family
  render(<AmsMappingEditor preview={{ required: [{ slot: 1, material: 'ASA_CF', color: '' }], available: [
    { slot: 1, catalogue_name: 'ASA-CF Invented Black', material: 'ASA_CF', color: '', color_hex: '2B2D31', spool_id: null },
    { slot: 4, material: 'empty', color: '', color_hex: '', spool_id: null },
  ], suggested_mapping: [] } as any} mapping={[]} enabled={false} valid={false} onChange={noop} onEnabledChange={noop} />)
  expect(screen.getByText('Filament 1: ASA-CF')).toBeInTheDocument()
  if (family === 'foundry') {
    const empty = screen.getByRole('button', { name: /AMS 1 · 4/ })
    expect(empty).toHaveTextContent('Empty')
    expect(empty).toHaveTextContent('No spool')
    expect(empty).not.toHaveTextContent('Loaded')
    expect(screen.getByRole('button', { name: /ASA-CF Invented Black.*ASA-CF/ })).toHaveTextContent('ASA-CF Invented Black ASA-CF')
  } else expect(screen.getByRole('option', { name: /ASA-CF Invented Black ASA-CF/ })).toBeInTheDocument()
})
it('slot renderers do not interpolate raw material properties as visible text', () => {
  for (const path of ['components/foundry/SlotTile.tsx', 'components/printers/PrinterCard.tsx', 'components/printers/PrinterPanels.tsx', 'components/ui/SpoolRing.tsx', 'components/shared/AmsMappingEditor.tsx']) {
    const source = readFileSync(`src/${path}`, 'utf8')
    expect(source, path).not.toMatch(/text=\{(?:slot\.(?:filament_type|material_type)|ext\.material)\}/)
    expect(source, path).not.toMatch(/slot\.catalogue_name\s*\|\|\s*slot\.material_type/)
    expect(source, path).not.toMatch(/>\s*\{(?:slot\.(?:filament_type|material_type)|req\.material|material)(?:\s*\|\|[^}]+)?\}\s*</)
  }
})

it.each(['ASA_CF', 'TPU_AMS', 'ASA_AERO', 'PET_CF', 'PPA_CF', 'PPS_CF'] as FilamentType[])('%s compact tiles use the short label and full tiles retain the long label', code => {
  const view = render(<SlotTile variant="compact" slot={{ slot_number: 1, filament_type: code }} />)
  expect(view.container.querySelector('.f-slot-label')).toHaveTextContent(code === 'TPU_AMS' ? 'TPU-AMS' : labels[code])
  view.rerender(<SlotTile expandable={false} slot={{ slot_number: 1, filament_type: code }} />)
  expect(view.container.querySelector('.f-slot-label')).toHaveTextContent(labels[code])
})
it.each([{ material: 'empty' }, { filament_type: 'empty', material: '' }])('Foundry preserves explicit empty mapping state: %j', empty => {
  document.documentElement.dataset.themeFamily = 'foundry'
  render(<AmsMappingEditor preview={{ required: [{ slot: 1, material: 'PLA', color: '' }], available: [{ slot: 4, color: '', color_hex: '', spool_id: null, ...empty }], suggested_mapping: [] } as any}
    mapping={[]} enabled={false} valid={false} onChange={noop} onEnabledChange={noop} />)
  const button = screen.getByRole('button', { name: 'AMS 1 · 4 (Empty)' })
  expect(button).toHaveTextContent('No spool')
  expect(button).not.toHaveTextContent('Loaded')
})
