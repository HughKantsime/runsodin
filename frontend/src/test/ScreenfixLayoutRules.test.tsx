import { readFileSync } from 'node:fs'
import postcss from 'postcss'
import { cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SpoolGrid from '../components/inventory/SpoolGrid'
vi.mock('../permissions', () => ({ canDo: () => true }))
vi.mock('../components/inventory/PurchaseEvidenceModal', () => ({ default: () => null }))
afterEach(cleanup)
const noop = () => {}
it.each([false, true])('spool grid reserves wider desktop cards (grouped=%s)', grouped => {
  const { container } = render(<SpoolGrid spools={[{ id: 1, filament_name: 'Support for PLA/PETG Nature' } as any]} sortBy={grouped ? 'printer' : 'name'} groupByPrinter={grouped} searchQuery="" selectedSpools={new Set()} onToggleSelect={noop} onLoad={noop} onUnload={noop} onUse={noop} onArchive={noop} onEdit={noop} onDry={noop} />)
  const grid = container.querySelector('.grid')!
  // 328px includes the 28px selection gutter, leaving at least 300px per card.
  expect(grid).toHaveClass('grid-cols-[repeat(auto-fill,minmax(min(100%,328px),1fr))]', 'gap-4')
  expect(container.querySelector('.pl-7')).not.toBeNull()
  expect(grid).not.toHaveClass('xl:grid-cols-4')
})
it('Foundry grid stretches the card inside its selection wrapper', () => {
  const rules = postcss.parse(readFileSync('src/foundry.css', 'utf8'))
  const declarations = (selector: string) => {
    const result: Record<string, string> = {}
    rules.walkRules(selector, rule => { rule.walkDecls(d => { result[d.prop] = d.value }) })
    return result
  }
  expect(declarations('.f-card-grid > .relative')).toMatchObject({ display: 'flex', 'flex-direction': 'column' })
  expect(declarations('.f-card-grid > .relative > .f-card')).toMatchObject({ flex: '1' })
})
