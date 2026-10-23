import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import FilamentSlotEditor from './FilamentSlotEditor'
vi.mock('../../api', () => ({ printers: { assignSlotSpool: vi.fn() } }))
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
