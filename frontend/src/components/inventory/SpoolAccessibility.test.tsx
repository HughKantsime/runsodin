import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SpoolGrid from './SpoolGrid'
import SpoolCard from './SpoolCard'
import { applyTheme } from '../../hooks/themePreferences'
import type { Spool } from '../../types'
vi.mock('../../permissions', () => ({ canDo: () => true }))
vi.mock('./PurchaseEvidenceModal', () => ({ default: () => null }))
afterEach(() => { cleanup(); applyTheme('dark', 'classic') })
it.each(['classic', 'foundry'] as const)('%s spool selection and label link have stable names with unchanged actions', family => {
  applyTheme('dark', family)
  const select = vi.fn(), noop = () => {}
  render(<SpoolGrid spools={[{ id: 1, filament_name: 'Support PLA', status: 'active', qr_code: 'review-1' } as Spool]} sortBy="name" groupByPrinter={false} searchQuery="" selectedSpools={new Set()} onToggleSelect={select} onLoad={noop} onUnload={noop} onUse={noop} onArchive={noop} onEdit={noop} onDry={noop} />)
  fireEvent.click(screen.getByRole('checkbox', { name: 'Select Support PLA' }))
  expect(select).toHaveBeenCalledWith(1)
  expect(screen.getByRole('link', { name: 'Print label for Support PLA' })).toHaveAttribute('href', '/api/spools/1/label')
})
it.each(['classic', 'foundry'] as const)('%s spool identity keeps all facts and separates Foundry ring and text', family => {
  applyTheme('dark', family)
  const noop = () => {}
  const { container } = render(<SpoolCard spool={{ id: 1, filament_brand: 'Review', filament_name: 'Blue PLA with a long name', filament_material: 'PLA', status: 'active', percent_remaining: 67 } as Spool} onLoad={noop} onUnload={noop} onUse={noop} onArchive={noop} onEdit={noop} onDry={noop} />)
  const title = screen.getByRole('heading')
  expect(screen.getByLabelText('67% remaining')).toBeInTheDocument()
  if (family === 'foundry') {
    expect(title).toHaveTextContent('Review Blue PLA with a long name')
    expect(title).not.toHaveTextContent('Unknown color')
    expect(title.nextElementSibling).toHaveTextContent('Unknown color')
    expect(title.nextElementSibling?.nextElementSibling).toHaveTextContent('PLA')
    expect(screen.getAllByText('PLA')).toHaveLength(1)
    expect(container.querySelector('.f-spool-ring')).not.toHaveTextContent('PLA')
  } else {
    expect(title).toHaveTextContent('Unknown color')
    expect(screen.getAllByText('PLA')).toHaveLength(2)
    expect(container.querySelector('.f-spool-copy')).toBeNull()
  }
})
