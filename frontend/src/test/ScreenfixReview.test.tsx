import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ExpandableNote from '../components/ui/ExpandableNote'
import { FilamentSlotsPanel } from '../components/printers/PrinterPanels'
import FilamentSlotEditor from '../components/printers/FilamentSlotEditor'
import PrinterCard from '../components/foundry/PrinterCard'
import SpoolCard from '../components/inventory/SpoolCard'
import AmsMappingEditor from '../components/shared/AmsMappingEditor'
vi.mock('../permissions', () => ({ canDo: () => true, getCurrentUser: () => ({ role: 'admin' }) }))
vi.mock('../components/inventory/PurchaseEvidenceModal', () => ({ default: () => null }))
const noop = () => {}
const name = 'ASA-CF Invented very long catalogue colour name'
it.each(['classic', 'foundry'])('disclosure dialogs in %s are named, trap focus, close on Escape and restore focus', family => {
  document.documentElement.dataset.themeFamily = family
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(32)
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(96)
  const disclosures = [
    { title: 'Full note', trigger: 'Show full note', ui: <ExpandableNote text={name} /> },
    { title: 'Filaments', trigger: 'Filaments', ui: <PrinterCard printer={{ id: 1, name: 'Printer', api_type: 'bambu', filament_slots: [{ slot_number: 1, filament_type: 'PLA' }] } as any} /> },
    { title: 'Filament name', trigger: 'Show full filament name', ui: <SpoolCard spool={{ id: 1, filament_name: name, status: 'active' } as any} onLoad={noop} onUnload={noop} onUse={noop} onArchive={noop} onEdit={noop} onDry={noop} /> },
  ]
  for (const { title, trigger, ui } of disclosures) {
    const view = mount(ui)
    const opener = screen.getByRole('button', { name: trigger })
    opener.focus()
    fireEvent.click(opener)
    const dialog = screen.getByRole('dialog', { name: title })
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    const close = within(dialog).getByRole('button', { name: 'Close' })
    expect(close).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(dialog.contains(document.activeElement)).toBe(true)
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(close).toHaveFocus()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(opener).toHaveFocus()
    view.unmount()
  }
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); delete document.documentElement.dataset.themeFamily })
function mount(ui: React.ReactNode) {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { enabled: false } } })}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>)
}
it.each([{ catalogue_name: name }, { material_type: 'PLA' }, { filament_type: 'PLA' }, { color_hex: '#123456' }, { color: 'Blue' }])('1: Classic detail and card agree a populated slot is not empty: %j', data => {
  const slot = { slot_number: 1, remaining: 50, ...data } as any
  const view = mount(<><FilamentSlotsPanel printer={{ filament_slots: [slot] } as any} /><FilamentSlotEditor slot={slot} printerId={1} allFilaments={[]} spools={[]} onSave={noop} /></>)
  expect(screen.queryByText('Empty')).toBeNull()
  const rings = view.container.querySelectorAll('svg[aria-label="50% remaining"]')
  expect(rings).toHaveLength(2)
  const colors = [...rings].map(ring => ring.querySelectorAll('circle')[1].getAttribute('stroke'))
  expect(colors[0]).toBe(colors[1])
  if (data.color_hex) expect(colors[0]).toBe('#123456')
})
it('1: truly empty Classic slots stay empty', () => {
  mount(<FilamentSlotsPanel printer={{ filament_slots: [{ slot_number: 1, filament_type: 'empty' }] } as any} />)
  expect(screen.getAllByText('Empty').length).toBeGreaterThan(0)
})
it.each(['classic', 'foundry'])('2/3: %s measures overflow, responds to resize, and opens outside the cell', family => {
  document.documentElement.dataset.themeFamily = family
  let clipped = false
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(32)
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(() => clipped ? 320 : 32)
  const disconnect = vi.fn()
  let resize = () => {}
  vi.stubGlobal('ResizeObserver', class { constructor(callback: () => void) { resize = callback } observe() {} disconnect = disconnect })
  const view = mount(<table><tbody><tr><td><ExpandableNote text="Ran out." /></td></tr></tbody></table>)
  expect(screen.queryByRole('button', { name: 'Show full note' })).toBeNull()
  clipped = true
  act(() => resize())
  const button = screen.getByRole('button', { name: 'Show full note' })
  expect(button.parentElement).toHaveClass('relative', 'h-5')
  expect(button).toHaveClass('absolute', 'bottom-0', 'min-h-11')
  fireEvent.click(button)
  const dialog = screen.getByRole('dialog', { name: 'Full note' })
  expect(dialog.closest('td')).toBeNull()
  expect(within(dialog).getByText('Ran out.')).toBeVisible()
  expect(view.container.querySelector('td p')).toHaveClass('line-clamp-2')
  fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }))
  clipped = false
  act(() => resize())
  expect(screen.queryByRole('button', { name: 'Show full note' })).toBeNull()
  view.unmount()
  expect(disconnect).toHaveBeenCalled()
})
it.each([4, 16])('4: %i compact slots show material and one card-level full-name disclosure', count => {
  document.documentElement.dataset.themeFamily = 'foundry'
  const slots = Array.from({ length: count }, (_, i) => ({ slot_number: i + 1, filament_type: 'ASA-CF', catalogue_name: `${name} ${i}`, color_hex: '123456' }))
  const view = mount(<PrinterCard printer={{ id: 1, name: 'Printer', api_type: 'bambu', filament_slots: slots } as any} />)
  const tiles = view.container.querySelectorAll('.f-slots-compact .f-slot')
  expect(tiles).toHaveLength(count)
  for (const tile of tiles) {
    expect(tile.tagName).not.toBe('BUTTON')
    expect(tile.querySelector('.f-slot-label')).toHaveTextContent(/^ASA-CF$/)
  }
  const button = screen.getByRole('button', { name: 'Filaments' })
  expect(button).toHaveAttribute('aria-expanded', 'false')
  fireEvent.click(button)
  const dialog = screen.getByRole('dialog', { name: 'Filaments' })
  for (const slot of slots) expect(within(dialog).getByText(slot.catalogue_name)).toBeVisible()
  expect(within(dialog).getAllByText('Loaded')).toHaveLength(count)
  fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }))
  expect(screen.queryByRole('dialog')).toBeNull()
})
it.each(['classic', 'foundry'])('5/6: %s spool title is reachable by tap and unknown color stays on one line', family => {
  document.documentElement.dataset.themeFamily = family
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(32)
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(96)
  mount(<SpoolCard spool={{ id: 1, filament_brand: 'Bambu Lab', filament_name: name, status: 'active' } as any} onLoad={noop} onUnload={noop} onUse={noop} onArchive={noop} onEdit={noop} onDry={noop} />)
  fireEvent.click(screen.getByRole('button', { name: 'Show full filament name' }))
  expect(within(screen.getByRole('dialog', { name: 'Filament name' })).getByText(`Bambu Lab ${name}`)).toBeVisible()
  expect(screen.getByText('Unknown color')).toHaveClass('whitespace-nowrap')
})
it('7: AMS selection shows the complete name without a nested expansion button', () => {
  document.documentElement.dataset.themeFamily = 'foundry'
  const onChange = vi.fn()
  mount(<AmsMappingEditor preview={{ required: [{ slot: 1, material: 'ASA-CF', color: null }], available: [{ slot: 1, material: 'ASA-CF', color: name, color_hex: '123456' }], suggested_mapping: [] } as any}
    mapping={[]} enabled={false} valid={false} onChange={onChange} onEnabledChange={noop} />)
  const button = screen.getByRole('button', { name: /AMS 1 · 1/ })
  expect(button.querySelector('button')).toBeNull()
  expect(button.querySelector('.f-slot')).toHaveAttribute('data-expanded', 'true')
  expect(button.querySelector('.f-slot-label')).toHaveTextContent(`${name} ASA-CF`)
  fireEvent.click(button)
  expect(onChange).toHaveBeenCalledExactlyOnceWith([{ source_slot: 1, target_slot: 1 }])
})

it.each(['classic', 'foundry'])('3: %s keeps a 900-character note clamped while displaying all text in a wide dialog', family => {
  document.documentElement.dataset.themeFamily = family
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(32)
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(320)
  const text = 'Long note '.repeat(90)
  const view = mount(<table><tbody><tr><td><ExpandableNote text={text} /></td></tr></tbody></table>)
  const collapsed = view.container.querySelector('td p')!
  expect(collapsed).toHaveClass('line-clamp-2')
  fireEvent.click(screen.getByRole('button', { name: 'Show full note' }))
  const dialog = screen.getByRole('dialog', { name: 'Full note' })
  expect(dialog.closest('table')).toBeNull()
  expect(dialog.querySelector('p')!.textContent).toBe(text)
  expect(dialog.querySelector('[tabindex="-1"]')).toHaveClass('max-w-2xl')
  expect(collapsed).toHaveClass('line-clamp-2')
  fireEvent.keyDown(document, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).toBeNull()
})
it.each(['classic', 'foundry'])('6: %s unknown color does not wrap and a fitting spool title needs no control', family => {
  document.documentElement.dataset.themeFamily = family
  mount(<SpoolCard spool={{ id: 1, filament_name: 'PLA', status: 'active' } as any} onLoad={noop} onUnload={noop} onUse={noop} onArchive={noop} onEdit={noop} onDry={noop} />)
  expect(screen.getByText('Unknown color')).toHaveClass('whitespace-nowrap')
  expect(screen.queryByRole('button', { name: 'Show full filament name' })).toBeNull()
  expect(screen.getByRole('heading').firstElementChild).toHaveClass('line-clamp-2', 'md:line-clamp-3')
})
