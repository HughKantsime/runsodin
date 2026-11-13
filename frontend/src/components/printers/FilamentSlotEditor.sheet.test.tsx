// Mobile web pass, slice 4: the filament picker gets phone-sheet classes (max-sm only); 640 px and wider keep today's classes.
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import FilamentSlotEditor from './FilamentSlotEditor'

vi.mock('../../api', () => ({ printers: { assignSlotSpool: vi.fn(), confirmTrayIdentity: vi.fn() }, spools: { create: vi.fn() } }))
vi.mock('../../permissions', () => ({ canDo: () => true, getCurrentUser: () => ({ role: 'operator' }) }))
afterEach(cleanup)
const desktop = (cls: string) => cls.split(/\s+/).filter(t => t && !t.startsWith('max-sm:')).join(' ')
const slot = { id: 1, slot_number: 1, color_hex: 'FFFFFF', material_type: 'PLA', remaining: 50, assigned_spool_id: null, spool_confirmed: false }

test('desktop classes unchanged, DOM order kept, phone sheet tokens, 44 px Cancel', () => {
  const view = render(<FilamentSlotEditor slot={slot} allFilaments={[]} spools={[]} printerId={1} onSave={vi.fn()} />)
  fireEvent.click(view.container.querySelector('.cursor-pointer')!)
  const dialog = screen.getByRole('dialog')
  expect(dialog.className).toContain('fixed inset-0 z-50 flex items-end sm:items-center justify-center')
  expect(dialog.children[0].className).toBe('absolute inset-0 bg-black/50')
  const panel = dialog.children[1] as HTMLElement
  expect(desktop(panel.className)).toBe('relative bg-[var(--brand-card-bg)] rounded-t-xl sm:rounded-md p-4 w-full sm:w-80 shadow-xl border border-[var(--brand-border)] max-h-[80vh] flex flex-col')
  expect(panel).toHaveClass('max-sm:h-full', 'max-sm:max-h-none', 'max-sm:rounded-none')
  const list = panel.children[2] as HTMLElement
  expect(desktop(list.className)).toBe('flex-1 overflow-y-auto space-y-1 max-h-96')
  expect(list).toHaveClass('max-sm:max-h-none', 'max-sm:min-h-0')
  const footer = screen.getByText('Cancel').parentElement!
  expect(footer).toHaveAttribute('data-modal-footer')
  expect(desktop(footer.className)).toBe('flex gap-2 mt-4 shrink-0')
  const cancel = screen.getByText('Cancel')
  expect(desktop(cancel.className)).toBe('flex-1 text-sm bg-[var(--brand-input-bg)] hover:brightness-110 rounded-md py-2')
  expect(cancel).toHaveClass('max-sm:min-h-11')
})
