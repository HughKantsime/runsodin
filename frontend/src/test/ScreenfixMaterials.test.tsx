import ts from 'typescript'
import { join } from 'node:path'
import { readFileSync, readdirSync } from 'node:fs'
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
// Search every component source, not a maintained list of renderers. Inspect text
// and display attributes, leaving raw API values, keys and form values alone.
// This is a syntax guard, not type/data-flow analysis: indirect props, helper
// return values, computed keys and material fields with other names need render tests.
function rawMaterialDisplays(source: string, path = 'fixture.tsx') {
  const file = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const fields = /^(material|material_type|filament_type|filament_material|default_filament_type)$/
  const initializers = new Map<string, ts.Expression>()
  const collect = (node: ts.Node) => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.initializer) initializers.set(node.name.text, node.initializer)
    ts.forEachChild(node, collect)
  }
  collect(file)
  const raw = (node: ts.Node, seen = new Set<string>()): boolean => {
    if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node) || ts.isJsxFragment(node)) return false
    if (ts.isCallExpression(node) && node.expression.getText(file) === 'materialLabel') return false
    if (ts.isPropertyAccessExpression(node)) {
      // Nozzle metallurgy is not a filament enum (e.g. hardened steel).
      return fields.test(node.name.text) && node.expression.getText(file) !== 'nozzle'
    }
    if (ts.isPropertyAssignment(node)) return raw(node.initializer, seen)
    if (ts.isIdentifier(node)) {
      if (seen.has(node.text)) return false
      const initializer = initializers.get(node.text)
      return initializer ? raw(initializer, new Set([...seen, node.text])) : fields.test(node.text)
    }
    if (ts.isConditionalExpression(node)) return raw(node.whenTrue, seen) || raw(node.whenFalse, seen)
    if (ts.isBinaryExpression(node)) {
      if (node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken) return raw(node.right, seen)
      if (![ts.SyntaxKind.BarBarToken, ts.SyntaxKind.QuestionQuestionToken, ts.SyntaxKind.PlusToken].includes(node.operatorToken.kind)) return false
    }
    return ts.forEachChild(node, child => raw(child, seen) || undefined) || false
  }
  const failures: string[] = []
  const visit = (node: ts.Node) => {
    if (ts.isJsxExpression(node) && node.expression) {
      const parent = node.parent
      const display = !ts.isJsxAttribute(parent) || /^(text|label|title|aria-label)$/.test(parent.name.getText(file))
      if (display && raw(node.expression)) failures.push(`${path}:${file.getLineAndCharacterOfPosition(node.getStart()).line + 1}: ${node.getText(file)}`)
    }
    ts.forEachChild(node, visit)
  }
  visit(file)
  return failures
}
it('all component material displays use the label function', () => {
  const sources = (dir: string): string[] => readdirSync(dir, { withFileTypes: true }).flatMap(entry => {
    const path = join(dir, entry.name)
    return entry.isDirectory() ? sources(path) : /\.[jt]sx$/.test(path) && !/\.test\.|__tests__/.test(path) ? [path] : []
  })
  const failures = sources('src/components').flatMap(path => {
    const source = readFileSync(path, 'utf8')
    return /material|filament_type/.test(source) ? rawMaterialDisplays(source, path) : []
  })
  expect(failures).toEqual([])
})
it('material guard catches text, fallbacks, aliases and accessible labels even beside a formatted value', () => {
  expect(rawMaterialDisplays('const label = row.material; const ui = <><td>{row.material}</td><span>{slot.catalogue_name || slot.material_type}</span><SlotLabel text={req.material} /><button aria-label={`Set threshold for ${row.material}`}>{materialLabel(row.material)}</button><p>{label}</p></>')).toHaveLength(5)
  expect(rawMaterialDisplays('const label = materialLabel(row.material); const ui = <><p>{label}</p><p>{row.material && materialLabel(row.material)}</p><option value={row.material}>{materialLabel(row.material)}</option><p>{nozzle.material}</p></>')).toEqual([])
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
