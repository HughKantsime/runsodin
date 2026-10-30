import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import FilamentSlotEditor from './FilamentSlotEditor'
import { printers } from '../../api'
vi.mock('../../api', () => ({ printers: { assignSlotSpool: vi.fn(), confirmTrayIdentity: vi.fn() }, spools: { create: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: () => true }))
afterEach(cleanup)
const slot = { id: 1, slot_number: 1, color_hex: 'FFFFFF', material_type: 'PLA', remaining: 50, assigned_spool_id: null, spool_confirmed: false }
for (const mode of ['hover', 'drag']) for (const dismiss of ['Escape', 'Cancel', 'backdrop']) {
  it(`portals and dismisses via ${dismiss} while card is ${mode}`, () => {
    const parentClick = vi.fn()
    const view = render(<div data-testid="card" style={{ filter: 'brightness(1.1)', transform: mode === 'drag' ? 'scale(.95)' : '' }} onClick={parentClick}>
      <FilamentSlotEditor slot={slot} allFilaments={[]} spools={[]} printerId={1} onSave={vi.fn()} />
    </div>)
    fireEvent.mouseEnter(screen.getByTestId('card'))
    fireEvent.click(view.container.querySelector('.cursor-pointer')!)
    parentClick.mockClear()
    const dialog = screen.getByRole('dialog')
    expect(dialog.parentElement).toBe(document.body)
    fireEvent.pointerDown(dialog)
    fireEvent.dragStart(dialog)
    if (dismiss === 'Escape') fireEvent.keyDown(document, { key: 'Escape' })
    else if (dismiss === 'Cancel') fireEvent.click(screen.getByText('Cancel'))
    else fireEvent.click(dialog.firstElementChild!)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(parentClick).not.toHaveBeenCalled()
  })
}


it('renders flagged tray fields and confirms the bound spool using the observed state', async () => {
  vi.mocked(printers.confirmTrayIdentity).mockResolvedValue({ spool_id: 56, replayed: false })
  const onSave = vi.fn()
  render(<FilamentSlotEditor slot={{ ...slot, assigned_spool_id: 56, remaining: null,
    color: 'Elegoo Rapid PETG', material_type: 'PETG', spool_change_pending: true, observed_state: 'a'.repeat(32), preset: 'preset: Bambu PETG HF' }}
    allFilaments={[]} spools={[]} printerId={1} onSave={onSave} />)
  expect(screen.getByText('Slot 1: spool may have changed?').parentElement).toHaveClass('col-span-full', 'min-w-0')
  expect(screen.getByText('Remaining unknown')).toBeInTheDocument()
  fireEvent.click(screen.getByText('Same spool'))
  await waitFor(() => expect(printers.confirmTrayIdentity).toHaveBeenCalledWith(1, 1,
    { observed_state: 'a'.repeat(32), decision: 'same' }))
  await waitFor(() => expect(onSave).toHaveBeenCalledWith({}))
  expect(screen.queryByText('Slot 1: spool may have changed?')).not.toBeInTheDocument()
})

it('clamps material to two lines and truncates the preset to one with full title tooltips', () => {
 const material = 'Support for PLA and PETG with a very long material name'
 const preset = 'BambuPETGVeryLongUnbrokenPresetNameForNarrowCards'
 render(<FilamentSlotEditor slot={{ ...slot, color: material, preset: `preset: ${preset}`, remaining: null }} allFilaments={[]} spools={[]} printerId={1} onSave={vi.fn()} />)
 for (const text of [material, 'Remaining unknown']) {
  const label = screen.getByTitle(text)
  expect(label).toHaveTextContent(text)
  expect(label).toHaveClass('line-clamp-2', 'overflow-hidden', 'w-full', 'min-w-0', '[overflow-wrap:anywhere]')
 }
 const presetLabel = screen.getByTitle(`preset: ${preset}`)
 expect(presetLabel).toHaveTextContent(`preset: ${preset}`)
 expect(presetLabel).toHaveClass('truncate', 'overflow-hidden', 'w-full', 'min-w-0')
 expect(presetLabel).not.toHaveClass('line-clamp-2')
 expect(screen.getByTitle(material)).not.toContainElement(screen.getByTitle(`preset: ${preset}`))
 expect(screen.getByTestId('filament-slot')).toHaveClass('min-w-0', 'overflow-hidden')
})
